"""Read-only audit for Guardian core-group and Telegram chat identifiers."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.group.models import Group
from app.modules.guardian.models import (
    GroupModerationPolicy,
    GroupPunishmentPolicy,
    GroupVerificationConfig,
    ManagedGroupBinding,
    ModerationRule,
    ModerationSensitiveKeyword,
    Violation,
    Whitelist,
)
from app.modules.owned_group.models import OwnedGroupAsset


@dataclass(frozen=True, slots=True)
class AuditFinding:
    """A safe finding containing identifiers local to Vanguard only."""

    reason_code: str
    entity: str
    record_ids: tuple[int, ...]
    related_record_ids: tuple[int, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "reason_code": self.reason_code,
            "entity": self.entity,
            "record_ids": list(self.record_ids),
        }
        if self.related_record_ids:
            payload["related_record_ids"] = list(self.related_record_ids)
        return payload


@dataclass(frozen=True, slots=True)
class GuardianGroupIdAuditReport:
    """Stable, secret-free result of a Guardian group ID audit."""

    scanned_counts: dict[str, int]
    findings: tuple[AuditFinding, ...]

    @property
    def ok(self) -> bool:
        return not self.findings

    @property
    def total_findings(self) -> int:
        return len(self.findings)

    @property
    def finding_counts(self) -> dict[str, int]:
        counts = Counter(finding.reason_code for finding in self.findings)
        return dict(sorted(counts.items()))

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "total_findings": self.total_findings,
            "scanned_counts": dict(sorted(self.scanned_counts.items())),
            "finding_counts": self.finding_counts,
            "findings": [finding.as_dict() for finding in self.findings],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True, slots=True)
class _GroupRow:
    id: int
    telegram_group_id: int


@dataclass(frozen=True, slots=True)
class _BindingRow:
    id: int
    group_id: int
    telegram_group_id: int
    bot_account_id: int


@dataclass(frozen=True, slots=True)
class _OwnedAssetRow:
    id: int
    telegram_chat_id: int | None
    core_group_id: int | None
    managed_binding_id: int | None
    guardian_bot_account_id: int | None
    governance_status: str


GROUP_SCOPED_MODELS = (
    (GroupVerificationConfig, "policy"),
    (GroupModerationPolicy, "policy"),
    (GroupPunishmentPolicy, "policy"),
    (ModerationRule, "policy"),
    (Whitelist, "policy"),
    (ModerationSensitiveKeyword, "policy"),
    (Violation, "violation"),
)


def _finding(
    reason_code: str,
    entity: str,
    record_ids: list[int] | tuple[int, ...],
    related_record_ids: list[int] | tuple[int, ...] = (),
) -> AuditFinding:
    return AuditFinding(
        reason_code=reason_code,
        entity=entity,
        record_ids=tuple(sorted(set(record_ids))),
        related_record_ids=tuple(sorted(set(related_record_ids))),
    )


def _audit_bindings(
    bindings: list[_BindingRow],
    groups_by_id: dict[int, _GroupRow],
) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    by_telegram_chat: dict[int, list[_BindingRow]] = defaultdict(list)
    for binding in bindings:
        by_telegram_chat[binding.telegram_group_id].append(binding)

        group = groups_by_id.get(binding.group_id)
        if group is None:
            findings.append(
                _finding(
                    "managed_binding_core_group_missing", "managed_group_binding", [binding.id]
                )
            )
        elif group.telegram_group_id != binding.telegram_group_id:
            findings.append(
                _finding(
                    "managed_binding_core_telegram_mismatch",
                    "managed_group_binding",
                    [binding.id],
                    [group.id],
                )
            )

    for duplicate_rows in by_telegram_chat.values():
        if len(duplicate_rows) < 2:
            continue
        binding_ids = [row.id for row in duplicate_rows]
        findings.append(
            _finding(
                "managed_binding_duplicate_telegram_chat",
                "managed_group_binding",
                binding_ids,
            )
        )
        if len({row.bot_account_id for row in duplicate_rows}) > 1:
            findings.append(
                _finding(
                    "managed_binding_multi_bot_for_telegram_chat",
                    "managed_group_binding",
                    binding_ids,
                )
            )
    return findings


def _audit_owned_assets(
    assets: list[_OwnedAssetRow],
    groups_by_id: dict[int, _GroupRow],
    bindings_by_id: dict[int, _BindingRow],
) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    for asset in assets:
        if asset.governance_status == "managed" and any(
            value is None
            for value in (
                asset.telegram_chat_id,
                asset.core_group_id,
                asset.managed_binding_id,
                asset.guardian_bot_account_id,
            )
        ):
            findings.append(
                _finding(
                    "owned_asset_managed_references_incomplete", "owned_group_assets", [asset.id]
                )
            )

        group = groups_by_id.get(asset.core_group_id) if asset.core_group_id is not None else None
        if asset.core_group_id is not None and group is None:
            findings.append(
                _finding("owned_asset_core_group_missing", "owned_group_assets", [asset.id])
            )
        elif group is not None and group.telegram_group_id != asset.telegram_chat_id:
            findings.append(
                _finding(
                    "owned_asset_core_group_chat_mismatch",
                    "owned_group_assets",
                    [asset.id],
                    [group.id],
                )
            )

        binding = (
            bindings_by_id.get(asset.managed_binding_id)
            if asset.managed_binding_id is not None
            else None
        )
        if asset.managed_binding_id is not None and binding is None:
            findings.append(
                _finding("owned_asset_managed_binding_missing", "owned_group_assets", [asset.id])
            )
            continue
        if binding is None:
            continue
        if binding.group_id != asset.core_group_id:
            findings.append(
                _finding(
                    "owned_asset_binding_core_group_mismatch",
                    "owned_group_assets",
                    [asset.id],
                    [binding.id],
                )
            )
        if binding.telegram_group_id != asset.telegram_chat_id:
            findings.append(
                _finding(
                    "owned_asset_binding_chat_mismatch",
                    "owned_group_assets",
                    [asset.id],
                    [binding.id],
                )
            )
        if binding.bot_account_id != asset.guardian_bot_account_id:
            findings.append(
                _finding(
                    "owned_asset_binding_bot_mismatch",
                    "owned_group_assets",
                    [asset.id],
                    [binding.id],
                )
            )
    return findings


async def _audit_policy_group_ids(
    db: AsyncSession,
    *,
    core_group_ids: set[int],
    telegram_to_core_ids: dict[int, set[int]],
) -> tuple[list[AuditFinding], dict[str, int]]:
    findings: list[AuditFinding] = []
    scanned_counts: dict[str, int] = {}

    for model, reason_namespace in GROUP_SCOPED_MODELS:
        table_name = model.__tablename__
        rows = list((await db.execute(select(model.id, model.group_id).order_by(model.id))).all())
        scanned_counts[table_name] = len(rows)
        for record_id, group_id in rows:
            if group_id is None:
                continue
            telegram_matches = telegram_to_core_ids.get(group_id, set())
            if group_id not in core_group_ids:
                reason_code = (
                    f"{reason_namespace}_group_id_legacy_telegram"
                    if telegram_matches
                    else f"{reason_namespace}_group_id_missing_core_group"
                )
                findings.append(_finding(reason_code, table_name, [record_id]))
            elif any(core_id != group_id for core_id in telegram_matches):
                findings.append(
                    _finding(
                        f"{reason_namespace}_group_id_ambiguous_core_or_telegram",
                        table_name,
                        [record_id],
                    )
                )

    return findings, scanned_counts


async def audit_guardian_group_ids(db: AsyncSession) -> GuardianGroupIdAuditReport:
    """Audit historical ID mappings without mutating database or external state."""

    group_rows = list((await db.execute(select(Group.id, Group.group_id).order_by(Group.id))).all())
    groups = [_GroupRow(id=row.id, telegram_group_id=row.group_id) for row in group_rows]
    groups_by_id = {group.id: group for group in groups}
    telegram_to_core_ids: dict[int, set[int]] = defaultdict(set)
    for group in groups:
        telegram_to_core_ids[group.telegram_group_id].add(group.id)

    binding_rows = list(
        (
            await db.execute(
                select(
                    ManagedGroupBinding.id,
                    ManagedGroupBinding.group_id,
                    ManagedGroupBinding.telegram_group_id,
                    ManagedGroupBinding.bot_account_id,
                ).order_by(ManagedGroupBinding.id)
            )
        ).all()
    )
    bindings = [
        _BindingRow(
            id=row.id,
            group_id=row.group_id,
            telegram_group_id=row.telegram_group_id,
            bot_account_id=row.bot_account_id,
        )
        for row in binding_rows
    ]
    bindings_by_id = {binding.id: binding for binding in bindings}

    asset_rows = list(
        (
            await db.execute(
                select(
                    OwnedGroupAsset.id,
                    OwnedGroupAsset.telegram_chat_id,
                    OwnedGroupAsset.core_group_id,
                    OwnedGroupAsset.managed_binding_id,
                    OwnedGroupAsset.guardian_bot_account_id,
                    OwnedGroupAsset.governance_status,
                ).order_by(OwnedGroupAsset.id)
            )
        ).all()
    )
    assets = [
        _OwnedAssetRow(
            id=row.id,
            telegram_chat_id=row.telegram_chat_id,
            core_group_id=row.core_group_id,
            managed_binding_id=row.managed_binding_id,
            guardian_bot_account_id=row.guardian_bot_account_id,
            governance_status=row.governance_status,
        )
        for row in asset_rows
    ]

    findings = _audit_bindings(bindings, groups_by_id)
    findings.extend(_audit_owned_assets(assets, groups_by_id, bindings_by_id))
    policy_findings, policy_counts = await _audit_policy_group_ids(
        db,
        core_group_ids=set(groups_by_id),
        telegram_to_core_ids=telegram_to_core_ids,
    )
    findings.extend(policy_findings)
    findings.sort(
        key=lambda item: (item.reason_code, item.entity, item.record_ids, item.related_record_ids)
    )

    return GuardianGroupIdAuditReport(
        scanned_counts={
            "group": len(groups),
            "managed_group_binding": len(bindings),
            "owned_group_assets": len(assets),
            **policy_counts,
        },
        findings=tuple(findings),
    )
