"""Durable, single-attempt semantic decisions with two bounded AI slots."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import fields
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.core.settings_models import SystemSetting
from app.modules.acquisition.group_qualification import URL

SEMANTIC_VERSION = "text-profile-redacted-v6-once"
PHONE = re.compile(r"(?<![\w])\+?\d[\d ()-]{5,}\d(?![\w])")
HANDLE = re.compile(r"(?<![\w/])@[a-zA-Z0-9_]{4,}")
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}")
SECRET = re.compile(r"(?:sk-[a-zA-Z0-9_-]{12,}|\b\d{6,}:[a-zA-Z0-9_-]{20,})")
MATERIAL_COLUMNS = {"source", "text", "sender_id", "sender_role", "message_id", "topic_id",
                    "reply_to_message_id", "system_account", "forwarded", "scope"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def profile_fingerprint(account: Any) -> str:
    return hashlib.sha256(str(getattr(account, "profile_bio", "") or "").encode()).hexdigest()


def anonymize_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep rule text and local index correspondence; never export real identifiers."""
    mappings: dict[str, dict[str, int]] = {}

    def token(kind: str, value: Any) -> int | None:
        if value is None:
            return None
        values = mappings.setdefault(kind, {})
        key = str(value)
        return values.setdefault(key, len(values) + 1)

    def text(value: Any) -> str:
        value = str(value or "")
        value = SECRET.sub("[credential_redacted]", value)
        value = re.sub(r"tg://user\?id=(\d+)", lambda m: f"[sender_{token('sender', m[1])}]", value)
        value = URL.sub(lambda m: f"https://target-{token('target', m[0])}.invalid", value)
        value = EMAIL.sub(lambda m: f"[email_{token('email', m[0])}]", value)
        value = HANDLE.sub(lambda m: f"@person_{token('handle', m[0].lower())}", value)
        value = PHONE.sub(lambda m: f"[number_{token('number', m[0])}]", value)
        for item in evidence:
            for field in ("sender_name", "sender_username", "phone", "group_title"):
                private = str(item.get(field) or "")
                if private:
                    value = value.replace(private, f"[identity_{token('identity', private)}]")
        return value

    chronology = sorted(
        {
            str(item.get(field))
            for item in evidence
            for field in ("date", "created_at", "edited_at")
            if item.get(field)
        }
    )
    result = []
    for item in evidence:
        safe = {
            key: item[key]
            for key in (
                "source",
                "sender_role",
                "scope",
                "age_hours",
                "system_account",
                "forwarded",
                "accessible",
                "content_forms",
                "classification",
                "verified_admin",
                "warning_search_complete",
            )
            if key in item
        }
        safe["text"] = text(item.get("text"))
        for field, kind in (
            ("sender_id", "sender"),
            ("message_id", "message"),
            ("reply_to_message_id", "message"),
            ("topic_id", "topic"),
        ):
            safe[field] = token(kind, item.get(field))
        safe["promotion_key"] = str(token("campaign", item.get("promotion_key")) or "")
        safe["promotion_targets"] = [
            str(token("target", v)) for v in item.get("promotion_targets", [])
        ]
        safe["association_key"] = token("association", item.get("association_key"))
        safe["warning_reply_ids"] = [token("message", v) for v in item.get("warning_reply_ids", [])]
        # Relative order is enough for superseded rules; absolute timestamps stay local.
        for field, source in (
            ("created_at", item.get("created_at") or item.get("date")),
            ("edited_at", item.get("edited_at")),
        ):
            safe[field] = f"timeline_{chronology.index(str(source)) + 1}" if source else None
        safe["edited"] = bool(item.get("edited_at"))
        result.append(safe)
    return result


def semantic_key(snapshot: dict, evidence: list[dict], account: Any, limits: dict) -> str:
    # Live rights, retention, model configuration and observation timestamps are
    # independent gates. Refreshing them must not purchase another recognition.
    semantic = [{key: value for key, value in item.items() if key in MATERIAL_COLUMNS}
                for item in snapshot.get("evidence", evidence)]
    value = {
        "key_version": "material-once-v1",
        "account": account.id,
        "bio": profile_fingerprint(account),
        "group": snapshot.get("raw_peer_id"),
        "group_type": snapshot.get("group_type"),
        "scope": "text_profile",
        "evidence": sorted(semantic, key=_json),
    }
    return hashlib.sha256(_json(value).encode()).hexdigest()


async def claim_slot(db: Any, *, now: datetime | None = None) -> tuple[str, str] | None:
    now = now or datetime.utcnow()
    for number in range(2):
        key = f"qualification.ai.slot.{number}"
        row = await db.get(SystemSetting, key, populate_existing=True)
        value = _json(
            {"token": uuid4().hex, "expires_at": (now + timedelta(seconds=300)).isoformat()}
        )
        if row is None:
            try:
                async with db.begin_nested():
                    db.add(SystemSetting(key=key, value=value))
                    await db.flush()
                await db.commit()
                return key, value
            except IntegrityError:
                continue
        try:
            expires = datetime.fromisoformat(json.loads(row.value).get("expires_at", ""))
        except (ValueError, TypeError, AttributeError):
            expires = datetime.min
        if expires > now:
            continue
        claimed = await db.execute(
            update(SystemSetting)
            .where(SystemSetting.key == key, SystemSetting.value == row.value)
            .values(value=value)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        if claimed.rowcount == 1:
            return key, value
    return None


async def release_slot(db: Any, claim: tuple[str, str]) -> None:
    # Exact token/value CAS prevents an expired holder releasing its successor.
    await db.execute(
        update(SystemSetting)
        .where(SystemSetting.key == claim[0], SystemSetting.value == claim[1])
        .values(value="{}")
        .execution_options(synchronize_session=False)
    )
    await db.commit()


def binary_result(result: Any, limits: dict) -> Any:
    passed = (result.ad_allowed is True
              and result.policy_mode in {"soft_ad_allowed", "soft_ad_trial"}
              and result.confidence >= max(95, int(limits.get("ad_policy_ai_min_confidence") or 95)))
    result.ad_allowed = bool(passed)
    if not passed:
        result.reason = result.reason or "group_rules_ai_not_passed"
        # A failed recognition is not proof of an actual group-wide prohibition.
        if result.policy_mode not in {"forbidden", "approval_required"}:
            result.policy_mode = "unknown"
    return result


def saved_result(result: Any) -> dict:
    return {field.name: getattr(result, field.name) for field in fields(result)
            if field.name not in {"evidence", "cache_hit"}}


async def review_semantics(
    service: Any, snapshot: dict, account: Any, local: Any, limits: dict,
    *, previous: dict | None = None,
) -> Any:
    from app.core.ai.llm_client import LLMClient
    from app.modules.acquisition.automation import GroupAdRulesAuditResult

    original = snapshot["evidence"]
    # Cached review indexes always refer to this stable order, including when
    # Telegram returns the same materials in a different order on the next read.
    source_order = {"full_about": 0, "pinned_message": 1, "admin_rule": 2, "group_profile": 3}
    original.sort(key=lambda item: (source_order.get(item.get("source"), 4),
                                   _json({k: v for k, v in item.items() if k in MATERIAL_COLUMNS})))
    safe = anonymize_evidence(original)
    snapshot["ai_redaction_version"] = SEMANTIC_VERSION
    snapshot["profile_fingerprint"] = profile_fingerprint(account)
    material = semantic_key(snapshot, safe, account, limits)
    snapshot.update(ai_material_key=material, ai_final=True, ai_pending=False, ai_decision="fail")
    cache_key = "qualification.ai.once." + material
    # Commit the terminal fail-closed attempt BEFORE any external call. A crash,
    # timeout or concurrent reader cannot reclaim it or invoke the provider again.
    failed = GroupAdRulesAuditResult(ad_allowed=False, reason="group_rules_ai_attempt_interrupted")
    old = (previous or {}).get("advertising_audit") or {}
    attempted = old.get("ai_reviews") or str(old.get("reason") or "").startswith("group_rules_ai_")
    reuse_old = bool(attempted and previous.get("profile_fingerprint") == profile_fingerprint(account)
                     and semantic_key(previous, [], account, limits) == material)
    if reuse_old:
        accepted = {field.name for field in fields(failed)} - {"evidence", "cache_hit"}
        failed = binary_result(GroupAdRulesAuditResult(**{k: v for k, v in old.items() if k in accepted}), limits)
    value = _json({"at": datetime.utcnow().isoformat(), "result": saved_result(failed),
                   "legacy_import": reuse_old})
    cache = await service.db.get(SystemSetting, cache_key, populate_existing=True)
    if cache is None:
        try:
            async with service.db.begin_nested():
                service.db.add(SystemSetting(key=cache_key, value=value))
                await service.db.flush()
            await service.db.commit()
        except IntegrityError:
            # Another worker won the unique insert.  Its fail-closed marker
            # already owns this material, so reload and reuse it; never call
            # the provider from the losing worker.
            cache = await service.db.get(SystemSetting, cache_key, populate_existing=True)
    if cache is not None or reuse_old:
        try:
            data = json.loads(cache.value if cache is not None else value)
            result = binary_result(GroupAdRulesAuditResult(**data["result"]), limits)
        except (ValueError, TypeError, KeyError):
            result = failed
        result.evidence, result.cache_hit = original, True
        snapshot["ai_decision"] = "pass" if result.ad_allowed else "fail"
        return result
    if not limits.get("ad_policy_ai_enabled", True):
        failed.reason = "group_rules_ai_disabled"
        return await finish_once(service.db, cache_key, failed, snapshot, original)
    llm = service._ad_policy_llm()
    # Scope actual configured clients to the one user-authorized destination.
    endpoint = urlsplit(str(getattr(llm, "base_url", "") or ""))
    approved_endpoint = endpoint.scheme == "https" and (
        endpoint.hostname == "api.pipenai.xyz"
        or (
            endpoint.hostname == "ark.cn-beijing.volces.com"
            and endpoint.path.rstrip("/") == "/api/coding/v3"
        )
    )
    if isinstance(llm, LLMClient) and not approved_endpoint:
        failed.reason = "group_rules_ai_destination_unapproved"
        return await finish_once(service.db, cache_key, failed, snapshot, original)
    claim = await claim_slot(service.db)
    if claim is None:
        failed.reason = "group_rules_ai_capacity_unavailable"
        return await finish_once(service.db, cache_key, failed, snapshot, original)
    try:
        async with asyncio.timeout(240):
            result = await service._evaluate_group_ad_rules_with_ai(safe, local, limits)
        return await finish_once(service.db, cache_key, binary_result(result, limits), snapshot, original)
    except Exception:
        failed.reason = "group_rules_ai_unavailable"
        return await finish_once(service.db, cache_key, failed, snapshot, original)
    finally:
        await release_slot(service.db, claim)


async def finish_once(db: Any, key: str, result: Any, snapshot: dict, evidence: list) -> Any:
    await db.execute(update(SystemSetting).where(SystemSetting.key == key).values(
        value=_json({"at": datetime.utcnow().isoformat(), "result": saved_result(result)})
    ).execution_options(synchronize_session=False))
    await db.commit()
    result.evidence = evidence
    snapshot["ai_decision"] = "pass" if result.ad_allowed is True else "fail"
    return result


async def retire_failed_retries(db: Any) -> dict:
    """Import existing failed recognitions without any external recognition/read."""
    from sqlalchemy import func, select

    from app.core.account.models import TelegramAccount
    from app.core.group.models import GroupAccountMembership as Member
    from app.modules.acquisition.adaptive_frequency import payload
    from app.modules.acquisition.automation import GroupAdRulesAuditResult
    from app.modules.acquisition.models import GroupQualificationAudit as Audit
    latest = select(func.max(Audit.id)).where(Audit.membership_id == Member.id,
        Audit.membership_joined_at == Member.joined_at, Audit.state != "cancelled").correlate(Member).scalar_subquery()
    rows = (await db.execute(select(Member, Audit).join(Audit, Audit.id == latest).where(
        Member.status == "joined", Member.left_at.is_(None),
        Audit.decision.notin_(["allowed", "trial", "protected"]),
    ).with_for_update(of=Audit, skip_locked=True).limit(500))).all()
    retired = []
    for member, row in rows:
        snapshot = payload(row.evidence_json)
        material = snapshot.get("pending_collection") or snapshot
        old = material.get("advertising_audit") or snapshot.get("advertising_audit") or {}
        reason = str(old.get("reason") or material.get("reason") or row.reason or "")
        if not reason.startswith("group_rules_ai_") or old.get("ad_allowed") is True:
            continue
        account = await db.get(TelegramAccount, row.account_id)
        if account is None or not material.get("evidence"):
            continue
        key = semantic_key(material, [], account, {})
        result = GroupAdRulesAuditResult(ad_allowed=False, reason=reason,
                                        decision_source="existing_recognition")
        if await db.get(SystemSetting, "qualification.ai.once." + key) is None:
            db.add(SystemSetting(key="qualification.ai.once." + key,
                value=_json({"at": datetime.utcnow().isoformat(), "result": saved_result(result), "legacy_import": True})))
        snapshot = {**snapshot, **material}
        snapshot.update(ai_final=True, ai_decision="fail", ai_pending=False, ai_review_incomplete=False,
                        ai_material_key=key, decision="reject", reason=reason, review_trigger="evidence_changed")
        snapshot["advertising_audit"] = {**old, "ad_allowed": False, "ai_decision": "fail"}
        # Preserve real Telegram exit facts; the negative AI result adds none.
        snapshot.pop("pending_collection", None)
        row.evidence_json = _json(snapshot)
        from app.modules.acquisition.evidence_progress import evidence_maturity
        maturity = evidence_maturity(snapshot, datetime.utcnow())
        if maturity:
            snapshot.update(next_evidence_maturity_at=maturity.isoformat(), review_trigger="ordinary_ad_maturity")
            row.evidence_json = _json(snapshot)
        row.decision, row.reason, row.state, row.next_retry_at = "reject", reason, "completed", maturity
        if member.review_status != "exit_pending":
            member.ad_status, member.review_next_at = "blocked", maturity
        retired.append(row.id)
    await db.commit()
    return {"retired_ai_retries": retired}
