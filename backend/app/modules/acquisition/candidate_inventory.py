"""Account-scoped, expiring hints for the join queue (never ad qualification)."""

import json
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.core.settings_models import SystemSetting
from app.modules.acquisition.candidate_preview import CandidatePreview
from app.modules.acquisition.evidence_progress import date


def key(account_id: int, group_id: int) -> str:
    return f"qualification.candidate_preview.{account_id}.{group_id}"


def matches(fact: dict, group: Any, account_id: int) -> bool:
    return (
        fact.get("version") == 1
        and fact.get("account_id") == account_id
        and fact.get("telegram_group_id") == group.group_id
        and fact.get("username") == group.username
    )


def fresh(fact: dict, group: Any, account_id: int, now: datetime) -> bool:
    checked, expires = date(fact.get("checked_at")), date(fact.get("expires_at", fact.get("next_preview_at")))
    return bool(
        matches(fact, group, account_id)
        and checked
        and expires
        and checked <= now < expires <= checked + timedelta(hours=6)
    )


def ready(fact: dict) -> bool:
    count = fact.get("member_count")
    return bool(
        not fact.get("preview_exclusion")
        and fact.get("namespace") in {"chat", "channel"}
        and type(count) is int
        and count >= 50
        and (
            int(fact.get("ordinary_advertisers") or 0) > 0
            or fact.get("rule_signal") == "explicit_allow"
        )
    )


def record(group: Any, account_id: int, hint: CandidatePreview, now: datetime) -> dict:
    collected = min(now, date(hint.evidence_collected_at) or now)
    fact = {
        **asdict(hint),
        "version": 1,
        "account_id": account_id,
        "telegram_group_id": group.group_id,
        "username": group.username,
        "namespace": hint.peer_namespace,
        "preview_exclusion": hint.exclusion_reason,
        "score": hint.score,
        "checked_at": collected.isoformat(),
    }
    ttl = (
        timedelta(hours=6)
        if hint.exclusion_reason
        else timedelta(hours=3) if ready(fact) else timedelta(minutes=15)
    )
    fact["expires_at"] = (collected + ttl).isoformat()
    fact["next_preview_at"] = fact["expires_at"]
    return fact


def due(fact: dict, group: Any, account_id: int, now: datetime) -> bool:
    if fresh(fact, group, account_id, now):
        return False
    retry = date(fact.get("next_preview_at")) if matches(fact, group, account_id) else None
    return retry is None or retry <= now


def deferred_fact(group: Any, account_id: int, previous: dict, retry_at: datetime, progress: dict | None = None) -> dict:
    fact = dict(previous) if matches(previous, group, account_id) else {
        "version": 1, "account_id": account_id, "telegram_group_id": group.group_id,
        "username": group.username, "status": "deferred", "expires_at": None,
    }
    # Retry timing must never extend the validity of previously sampled facts.
    fact.setdefault("expires_at", fact.get("next_preview_at"))
    fact["next_preview_at"] = retry_at.isoformat()
    if progress is not None:
        fact["progress"] = progress
    return fact


async def load(db: Any, account_id: int, group_ids: list[int]) -> dict[int, dict]:
    keys = {key(account_id, group_id): group_id for group_id in group_ids}
    if not keys:
        return {}
    rows = (await db.scalars(select(SystemSetting).where(SystemSetting.key.in_(keys)))).all()
    result = {}
    for row in rows:
        try:
            fact = json.loads(row.value)
            if isinstance(fact, dict):
                result[keys[row.key]] = fact
        except (ValueError, TypeError):
            continue
    return result


async def save(db: Any, account_id: int, group_id: int, fact: dict) -> None:
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    keys = [key(account_id, group_id)]
    if fact.get("namespace") in {"chat", "channel"}:
        keys.append(f"qualification.candidate_identity.{group_id}")
    for record_key in keys:
        statement = insert(SystemSetting).values(key=record_key, value=json.dumps(fact))
        await db.execute(
            statement.on_conflict_do_update(
                index_elements=[SystemSetting.key], set_={"value": json.dumps(fact)}
            )
        )

async def account_retry_at(db: Any, account_id: int) -> datetime | None:
    row = await db.get(SystemSetting, f"qualification.candidate_retry.{account_id}")
    return date(row.value) if row else None


async def defer_account(db: Any, account_id: int, retry_at: datetime) -> None:
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    statement = insert(SystemSetting).values(key=f"qualification.candidate_retry.{account_id}", value=retry_at.isoformat())
    await db.execute(statement.on_conflict_do_update(index_elements=[SystemSetting.key], set_={"value": retry_at.isoformat()}))
