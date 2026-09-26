"""Scoped semantic review cache and two durable, bounded AI slots."""

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
from app.modules.acquisition.group_qualification import POLICY_VERSION, URL

SEMANTIC_VERSION = "text-profile-redacted-v5-one-ad"
PHONE = re.compile(r"(?<![\w])\+?\d[\d ()-]{5,}\d(?![\w])")
HANDLE = re.compile(r"(?<![\w/])@[a-zA-Z0-9_]{4,}")
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}")
SECRET = re.compile(r"(?:sk-[a-zA-Z0-9_-]{12,}|\b\d{6,}:[a-zA-Z0-9_-]{20,})")


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
    semantic = []
    for item in evidence:
        semantic.append(
            {
                key: (float(value or 0) >= 24 if key == "age_hours" else value)
                for key, value in item.items()
                if key not in {"observed_at", "age_hours"}
            }
            | {"retained_24h": float(item.get("age_hours") or 0) >= 24}
        )
    value = {
        "version": POLICY_VERSION,
        "semantic_version": SEMANTIC_VERSION,
        "account": account.id,
        "bio": profile_fingerprint(account),
        "risk_version": {
            field: str(getattr(account, field, None))
            for field in (
                "last_risk_event_at",
                "spam_checked_at",
                "restriction_detected_at",
                "risk_pause_until",
            )
        },
        "account_status": str(getattr(account, "status", None)),
        "risk_level": str(getattr(account, "risk_level", None)),
        "group": snapshot.get("raw_peer_id"),
        "group_type": snapshot.get("group_type"),
        "permissions": snapshot.get("permissions"),
        "scope": "text_profile",
        "model": limits.get("ad_policy_ai_model"),
        "threshold": limits.get("ad_policy_ai_min_confidence"),
        "evidence": semantic,
        "rule_edits": [
            (e.get("message_id"), e.get("edited_at"))
            for e in snapshot.get("evidence", [])
            if e.get("source") in {"pinned_message", "admin_rule"}
        ],
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


async def review_semantics(
    service: Any, snapshot: dict, account: Any, local: Any, limits: dict
) -> Any:
    from app.core.ai.llm_client import LLMClient
    from app.modules.acquisition.automation import GroupAdRulesAuditResult

    original = snapshot["evidence"]
    safe = anonymize_evidence(original)
    snapshot["ai_redaction_version"] = SEMANTIC_VERSION
    snapshot["profile_fingerprint"] = profile_fingerprint(account)
    if not limits.get("ad_policy_ai_enabled", True):
        local.ad_allowed, local.policy_mode, local.reason = (
            None,
            "unknown",
            "group_rules_ai_disabled",
        )
        snapshot["ai_pending"] = True
        return local
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
        local.ad_allowed, local.policy_mode, local.reason = (
            None,
            "unknown",
            "group_rules_ai_destination_unapproved",
        )
        snapshot["ai_pending"] = True
        return local
    cache_key = "qualification.ai.cache." + semantic_key(snapshot, safe, account, limits)
    cache = await service.db.get(SystemSetting, cache_key, populate_existing=True)
    now = datetime.utcnow()
    if cache:
        try:
            cached = json.loads(cache.value)
            if datetime.fromisoformat(cached["expires_at"]) > now:
                result = GroupAdRulesAuditResult(**cached["result"])
                result.evidence, result.cache_hit = original, True
                snapshot["ai_pending"] = False
                return result
        except (ValueError, TypeError, KeyError):
            pass
    claim = await claim_slot(service.db)
    if claim is None:
        local.ad_allowed, local.policy_mode, local.reason = None, "unknown", "group_rules_ai_queued"
        snapshot["ai_pending"] = True
        return local
    try:
        async with asyncio.timeout(240):
            result = await service._evaluate_group_ad_rules_with_ai(safe, local, limits)
        result.evidence = original
        pending = result.reason in {
            "group_rules_ai_unavailable",
            "group_rules_ai_disabled",
            "group_rules_ai_queued",
        }
        snapshot["ai_pending"] = pending
        if (
            not pending
            and result.ai_reviews
            and result.confidence >= 95
            and result.policy_mode
            in {"soft_ad_allowed", "soft_ad_trial", "forbidden", "approval_required"}
        ):
            saved = {
                field.name: getattr(result, field.name)
                for field in fields(result)
                if field.name not in {"evidence", "cache_hit"}
            }
            value = _json({"expires_at": (now + timedelta(hours=24)).isoformat(), "result": saved})
            cache = await service.db.get(SystemSetting, cache_key, populate_existing=True)
            if cache is None:
                try:
                    async with service.db.begin_nested():
                        service.db.add(SystemSetting(key=cache_key, value=value))
                        await service.db.flush()
                except IntegrityError:
                    pass
            else:
                cache.value = value
            await service.db.commit()
        return result
    except Exception:
        local.ad_allowed, local.policy_mode, local.reason = (
            None,
            "unknown",
            "group_rules_ai_unavailable",
        )
        snapshot["ai_pending"] = True
        return local
    finally:
        await release_slot(service.db, claim)
