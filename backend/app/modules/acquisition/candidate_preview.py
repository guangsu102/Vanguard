"""Bounded public, read-only hints for ordering join candidates.

A preview is never a qualification audit and never authorizes advertising.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest
from telethon.tl.types import InputMessagesFilterPinned

from app.core.account.rpc_governor import RpcDeferred
from app.modules.acquisition.group_qualification import (
    NEGATED_BAN,
    RULE,
    TOPIC_CONDITION,
    EvidenceCollector,
    evidence_age,
    looks_like_ad,
    naive,
    promotion_targets,
    text_of,
)
from app.modules.acquisition.qualification_identity import entity_identity, peer_identity
from app.modules.acquisition.search.group_finder import (
    is_joinable_telegram_entity,
    raise_if_flood_wait,
)

PREVIEW_GROUP_LIMIT = 6
PREVIEW_MESSAGE_LIMIT = 40
PREVIEW_PIN_LIMIT = 9
PREVIEW_ROLE_LIMIT = 4
ALLOW = re.compile(
    r"(?:允许|允許|可发|可發|欢迎|歡迎).{0,10}(?:广告|廣告|推广|推廣)"
    r"|(?:ads?|promotion|advertising)\s+(?:are\s+)?allowed",
    re.I,
)
BAN = re.compile(
    r"(?:禁止|严禁|嚴禁|不准|不许|不許|不允许|不允許|请勿|請勿).{0,14}"
    r"(?:广告|廣告|推广|推廣)|(?:no|ban)\s+(?:ads?|advertising|promotion)",
    re.I,
)
APPROVAL = re.compile(
    r"(?:广告|廣告|推广|推廣).{0,16}(?:先|需|须|須|联系|聯繫).{0,10}"
    r"(?:管理员|管理員|审批|審批|批准|付费|付費)|"
    r"(?:contact|ask)\s+(?:admin|owner).{0,16}(?:ads?|promotion)",
    re.I,
)


@dataclass(frozen=True)
class CandidatePreview:
    status: str
    rules_readable: bool = False
    rule_signal: str = "unknown"
    ordinary_advertisers: int = 0
    independent_ads: bool = False
    peer_namespace: str | None = None
    member_count: int | None = None
    online_count: int | None = None
    member_count_source: str = "unknown"
    evidence_collected_at: str | None = None

    @property
    def exclusion_reason(self) -> str | None:
        if self.status in {"identity_mismatch", "not_joinable"}:
            return self.status
        if self.member_count is not None and self.member_count < 50:
            return "prejoin_members_below_50"
        if (
            self.rule_signal == "explicit_ban"
            and self.online_count is not None
            and self.online_count < 2
        ):
            return "prejoin_rules_ban_and_online_below_2"
        return None

    @property
    def score(self) -> int:
        if self.exclusion_reason:
            return -100
        # One verified ordinary-member ad aged 24h takes precedence over rules.
        if self.ordinary_advertisers:
            return 10
        if self.rule_signal == "explicit_allow":
            return 5
        if self.rule_signal in {"restriction", "explicit_ban"}:
            return -7
        return 1 if self.rules_readable else 0


def independent_ads(items: list[tuple[int, set[str]]]) -> bool:
    """Transitive shared contacts denote one campaign, even across senders."""
    parent = list(range(len(items)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    owners: dict[str, int] = {}
    for index, (_, targets) in enumerate(items):
        for target in targets:
            if target in owners:
                parent[root(index)] = root(owners[target])
            else:
                owners[target] = index
    return any(
        first_sender > 0
        and second_sender > 0
        and first_sender != second_sender
        and first_targets
        and second_targets
        and root(first) != root(second)
        for first, (first_sender, first_targets) in enumerate(items)
        for second, (second_sender, second_targets) in enumerate(items)
        if first < second
    )


async def public_candidate_preview(
    client: Any,
    group: Any,
    *,
    own_user_ids: set[int],
    identity_coverage: bool,
    now: datetime | None = None,
    progress: dict | None = None,
    checkpoint: Callable[[dict], Awaitable[None]] | None = None,
) -> CandidatePreview:
    """Read a small public sample; unknown data never becomes a negative fact."""
    now = naive(now) or datetime.utcnow()
    try:
        entity = await client.get_entity(group.username)
        actual = entity_identity(entity)
        expected = peer_identity(group.group_id)
        if (
            actual is None
            or expected is None
            or actual[0] != expected[0]
            or (expected[1] is not None and actual[1] != expected[1])
        ):
            return CandidatePreview(status="identity_mismatch")
        if not is_joinable_telegram_entity(entity):
            return CandidatePreview(status="not_joinable")
    except Exception as exc:
        if isinstance(exc, RpcDeferred):
            raise
        raise_if_flood_wait(exc, operation="join_candidate_preview_entity")
        return CandidatePreview(status="entity_unknown")

    saved = progress or {}
    checked = saved.get("checked_at")
    try:
        valid = checked and now - timedelta(hours=3) <= datetime.fromisoformat(checked) <= now
    except (TypeError, ValueError):
        valid = False
    reusable = bool(valid and saved.get("namespace") == actual[1] and saved.get("entity_id") == actual[0])
    facts = dict(saved.get("facts") or {}) if reusable else {"peer_namespace": actual[1]}
    facts["evidence_collected_at"] = saved["checked_at"] if reusable else now.isoformat()
    rules_readable = bool(saved.get("rules_readable")) if reusable else False
    rule_signal = saved.get("rule_signal", "unknown") if reusable else "unknown"
    if not reusable:
        try:
            response = await client(
                GetFullChannelRequest(entity)
                if hasattr(entity, "megagroup")
                else GetFullChatRequest(entity.id)
            )
            full = response.full_chat
            count = getattr(full, "participants_count", None)
            source = "full_chat.participants_count"
            if not isinstance(count, int) or isinstance(count, bool):
                participants = getattr(getattr(full, "participants", None), "participants", None)
                count = len(participants) if isinstance(participants, list) else None
                source = "full_chat.participants"
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                facts.update(member_count=count, member_count_source=source)
            online = getattr(full, "online_count", None)
            if isinstance(online, int) and not isinstance(online, bool) and online >= 0:
                facts["online_count"] = online
            if facts.get("member_count", 50) < 50:
                return CandidatePreview(status="sampled", **facts)
            about = str(getattr(full, "about", "") or "").strip()
            pins = [
                item
                async for item in client.iter_messages(
                    entity, filter=InputMessagesFilterPinned(), limit=PREVIEW_PIN_LIMIT
                )
            ]
            complete = len(pins) < PREVIEW_PIN_LIMIT and not any(
                getattr(item, "media", None) and not text_of(item) for item in pins
            )
            combined = "\n".join([about, *(text_of(item) for item in pins)])
            rules_readable = complete and bool(
                RULE.search(combined)
                or ALLOW.search(combined)
                or BAN.search(combined)
                or APPROVAL.search(combined)
            )
            if rules_readable:
                without_negated_bans = NEGATED_BAN.sub("", combined)
                if BAN.search(without_negated_bans):
                    rule_signal = "explicit_ban"
                elif APPROVAL.search(combined) or TOPIC_CONDITION.search(combined):
                    rule_signal = "restriction"
                elif ALLOW.search(combined):
                    rule_signal = "explicit_allow"
                else:
                    rule_signal = "no_clear_signal"
        except Exception as exc:
            if isinstance(exc, RpcDeferred):
                raise
            raise_if_flood_wait(exc, operation="join_candidate_preview_rules")
            rules_readable = False
        if checkpoint is not None and rules_readable:
            await checkpoint({"checked_at": now.isoformat(), "namespace": actual[1], "entity_id": actual[0],
                              "facts": facts, "rules_readable": rules_readable, "rule_signal": rule_signal})

    if not identity_coverage:
        return CandidatePreview(
            status="identity_unconfirmed",
            rules_readable=rules_readable,
            rule_signal=rule_signal,
            **facts,
        )

    try:
        messages = [
            item
            async for item in client.iter_messages(
                entity,
                limit=PREVIEW_MESSAGE_LIMIT,
                offset_date=(now - timedelta(hours=24)).replace(tzinfo=UTC),
            )
        ]
    except Exception as exc:
        if isinstance(exc, RpcDeferred):
            raise
        raise_if_flood_wait(exc, operation="join_candidate_preview_history")
        return CandidatePreview(
            status="history_unknown",
            rules_readable=rules_readable,
            rule_signal=rule_signal,
            **facts,
        )
    collector = EvidenceCollector(client, own_user_ids=own_user_ids)
    observed: list[tuple[int, set[str]]] = []
    roles_checked = 0
    for message in messages:
        date = naive(getattr(message, "date", None))
        if (
            date is None
            or date < now - timedelta(hours=72)
            or getattr(message, "action", None)
            or getattr(getattr(message, "reply_to", None), "reply_to_top_id", None)
            or not looks_like_ad(text_of(message))
            or (evidence_age(message, now) or 0) < 24
        ):
            continue
        if roles_checked >= PREVIEW_ROLE_LIMIT:
            break
        roles_checked += 1
        try:
            role = await collector.role(entity, message)
        except Exception as exc:
            if isinstance(exc, RpcDeferred):
                raise
            raise_if_flood_wait(exc, operation="join_candidate_preview_role")
            return CandidatePreview(
                status="identity_unknown",
                rules_readable=rules_readable,
                rule_signal=rule_signal,
                **facts,
            )
        if role == "ordinary":
            observed.append((message.sender_id, promotion_targets(text_of(message))))
    return CandidatePreview(
        status="sampled",
        **facts,
        rules_readable=rules_readable,
        rule_signal=rule_signal,
        ordinary_advertisers=len({sender for sender, _ in observed}),
        independent_ads=independent_ads(observed),
    )
