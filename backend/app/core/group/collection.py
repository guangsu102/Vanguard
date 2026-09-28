"""Verified Telegram metadata, separate from qualification and marketing metrics."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import String, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest, GetOnlinesRequest

from app.core.account.models import AccountStatus, TelegramAccount
from app.core.group.models import Group, GroupAccountMembership
from app.core.settings_models import SystemSetting

PREFIX = "group.collection."
FRESH_HOURS = 6


def date(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None) if value else None
    except (TypeError, ValueError):
        return None


def metadata(value: str | None, now: datetime | None = None) -> dict:
    try:
        result = json.loads(value or "{}")
        if not isinstance(result, dict):
            result = {}
    except (TypeError, ValueError):
        result = {}
    now = now or datetime.utcnow()
    checked = date(result.get("member_count_checked_at"))
    result["stale"] = (
        not checked or checked < now - timedelta(hours=FRESH_HOURS) or bool(result.get("error"))
    )
    result["status"] = "stale" if result["stale"] else "fresh"
    return result


async def record_snapshot(
    db: AsyncSession, group: Group, snapshot: dict, *, source: str, error: str | None = None
) -> bool:
    """Caller owns commit. Older/failed collections never erase verified facts."""
    await db.scalar(select(Group.id).where(Group.id == group.id).with_for_update())
    key = PREFIX + str(group.group_id)
    row = await db.get(SystemSetting, key)
    result = metadata(row.value if row else None)
    at = date(snapshot.get("collected_at")) or datetime.utcnow()
    changed = False
    for field in ("member_count", "online_count"):
        value = snapshot.get(field)
        verified = (
            snapshot.get("member_count_verified") is True
            if field == "member_count"
            else snapshot.get("online_count_source") not in {None, "unknown"}
        )
        old_at = date(result.get(field + "_checked_at"))
        if (
            verified
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
            and (old_at is None or at > old_at)
        ):
            result[field] = value
            result[field + "_source"] = (
                source + ":" + str(snapshot.get(field + "_source", "verified"))
            )
            result[field + "_checked_at"] = at.isoformat()
            if field == "member_count":
                group.member_count = value
            changed = True
    previous_attempt = date(result.get("attempted_at"))
    if previous_attempt is None or at >= previous_attempt:
        result.update(
            attempted_at=at.isoformat(),
            last_result="updated" if changed else ("failed" if error else "skipped"),
            error=error,
        )
    if row is None:
        row = SystemSetting(key=key, value="{}")
        db.add(row)
    row.value = json.dumps(result, ensure_ascii=False)
    row.updated_at = datetime.utcnow()
    return changed


async def collection_for_groups(db: AsyncSession, targets: list[int]) -> dict[int, dict]:
    keys = [PREFIX + str(target) for target in targets]
    rows = (
        (await db.scalars(select(SystemSetting).where(SystemSetting.key.in_(keys)))).all()
        if keys
        else []
    )
    return {int(row.key[len(PREFIX) :]): metadata(row.value) for row in rows}


async def sync_stale_groups(
    db: AsyncSession, *, limit: int = 5, pool: Any = None
) -> dict[str, Any]:
    """A shared six-hour refresh budget; no history scanning, joins or sends."""
    from app.core.account.pool import (
        AccountOperationLeaseBusy,
        AccountOperationLeaseUnavailable,
        get_account_pool,
    )
    from app.modules.acquisition.qualification_identity import entity_identity, peer_identity

    pool = pool or get_account_pool()
    now = datetime.utcnow()
    # Correlate on the local group ID; a left join orders never-collected groups first.
    joined = (
        select(GroupAccountMembership.id)
        .where(
            GroupAccountMembership.group_id == Group.id, GroupAccountMembership.status == "joined"
        )
        .exists()
    )
    rows = list(
        (
            await db.scalars(
                select(Group)
                .outerjoin(
                    SystemSetting, SystemSetting.key == PREFIX + cast(Group.group_id, String)
                )
                .where(
                    joined,
                    or_(
                        SystemSetting.key.is_(None),
                        SystemSetting.updated_at <= now - timedelta(hours=FRESH_HOURS),
                    ),
                )
                .order_by(SystemSetting.updated_at.asc().nullsfirst(), Group.id)
                .limit(max(1, min(5, limit)))
            )
        ).all()
    )
    counts = {"checked": 0, "updated": 0, "skipped": 0, "failed": 0, "details": []}
    for group in rows:
        counts["checked"] += 1
        wrapper = None
        try:
            account = await db.scalar(
                select(TelegramAccount)
                .join(
                    GroupAccountMembership, GroupAccountMembership.account_id == TelegramAccount.id
                )
                .where(
                    GroupAccountMembership.group_id == group.id,
                    GroupAccountMembership.status == "joined",
                    TelegramAccount.is_active.is_(True),
                    TelegramAccount.status.in_([AccountStatus.ONLINE, AccountStatus.OFFLINE]),
                    or_(
                        TelegramAccount.risk_pause_until.is_(None),
                        TelegramAccount.risk_pause_until <= now,
                    ),
                    TelegramAccount.risk_level.notin_(["frozen", "quarantined"]),
                )
                .order_by(TelegramAccount.id)
                .limit(1)
            )
            if account is None:
                counts["skipped"] += 1
                counts["details"].append(
                    {"group_id": group.id, "result": "skipped", "reason": "account_unavailable"}
                )
                await record_snapshot(db, group, {}, source="sync", error="account_unavailable")
                await db.commit()
                continue
            from app.core.account.models import AccountOperationConfig
            operation = await db.scalar(select(AccountOperationConfig).where(
                AccountOperationConfig.account_id == account.id))
            if operation is not None and operation.dynamic_capacity_enabled:
                from app.modules.acquisition.ad_output_plan import ad_output_plan
                plan = await ad_output_plan(db, account, operation, now)
                if plan["join_blocker"] in {
                    "join_wait_ad_delivery", "join_wait_ad_survival", "join_wait_ad_reconciliation",
                    "account_risk_quarantined", "join_ad_account_unavailable",
                    "telegram_read_budget", "telegram_rpc_cooldown", "telegram_rpc_guard_unavailable",
                }:
                    counts["skipped"] += 1
                    counts["details"].append({"group_id": group.id, "result": "deferred",
                                               "reason": plan["join_blocker"]})
                    # A scheduling deferral must not overwrite fresh group evidence.
                    continue
            await pool.sync_from_db([account])
            wrapper = await pool.acquire_by_id(
                account.id, purpose="group_metadata_sync", raise_on_lease_failure=True
            )
            if wrapper is None:
                raise RuntimeError("account_unavailable")
            current = await db.get(
                SystemSetting, PREFIX + str(group.group_id), populate_existing=True
            )
            if (
                current
                and current.updated_at
                and current.updated_at > now - timedelta(hours=FRESH_HOURS)
            ):
                counts["skipped"] += 1
                counts["details"].append(
                    {
                        "group_id": group.id,
                        "result": "skipped",
                        "reason": "refreshed_by_another_task",
                    }
                )
                await db.commit()
                continue
            async with asyncio.timeout(15):
                client = wrapper.client
                entity = await client.get_entity(group.username or group.group_id)
                actual, expected = entity_identity(entity), peer_identity(group.group_id)
                if (
                    not actual
                    or not expected
                    or actual[0] != expected[0]
                    or (expected[1] is not None and actual[1] != expected[1])
                ):
                    raise RuntimeError("group_identity_mismatch")
                response = await client(
                    GetFullChannelRequest(entity)
                    if hasattr(entity, "megagroup")
                    else GetFullChatRequest(entity.id)
                )
                full = response.full_chat
                count = getattr(full, "participants_count", None)
                count_source = "full_chat.participants_count"
                if count is None:
                    participants = getattr(
                        getattr(full, "participants", None), "participants", None
                    )
                    if isinstance(participants, list):
                        count, count_source = len(participants), "full_chat.participants"
                if count is None:
                    matched = next(
                        (c for c in getattr(response, "chats", []) if entity_identity(c) == actual),
                        None,
                    )
                    count, count_source = (
                        getattr(matched, "participants_count", None),
                        "full_response.chat.participants_count",
                    )
                online = getattr(full, "online_count", None)
                online_source = "full_chat.online_count"
                if online is None:
                    try:
                        online = getattr(await client(GetOnlinesRequest(entity)), "onlines", None)
                        online_source = "messages.getOnlines"
                    except Exception as exc:
                        if "Flood" in type(exc).__name__:
                            raise
                snap = {
                    "collected_at": datetime.utcnow().isoformat(),
                    "member_count": count,
                    "member_count_verified": count is not None,
                    "member_count_source": count_source,
                    "online_count": online,
                    "online_count_source": online_source if online is not None else "unknown",
                }
                changed = await record_snapshot(
                    db,
                    group,
                    snap,
                    source="telegram_sync",
                    error="member_count_unavailable" if count is None else None,
                )
                if getattr(entity, "title", None):
                    group.title = str(entity.title)[:255]
            counts["updated" if changed else "failed"] += 1
            counts["details"].append(
                {"group_id": group.id, "result": "updated" if changed else "failed"}
            )
            await db.commit()
        except (AccountOperationLeaseBusy, AccountOperationLeaseUnavailable):
            counts["skipped"] += 1
            counts["details"].append(
                {"group_id": group.id, "result": "skipped", "reason": "account_lease_busy"}
            )
            await db.commit()
        except Exception as exc:
            reason = type(exc).__name__
            counts["failed"] += 1
            counts["details"].append({"group_id": group.id, "result": "failed", "reason": reason})
            await record_snapshot(db, group, {}, source="telegram_sync", error=reason)
            if "Flood" in reason:
                pause = datetime.utcnow() + timedelta(
                    seconds=max(60, int(getattr(exc, "seconds", 60))) + 60
                )
                account.risk_pause_until = max(account.risk_pause_until or pause, pause)
                if account.risk_level == "normal" and not account.risk_score:
                    account.risk_reason = "telegram_read_flood_wait"
                    account.last_risk_event_at = datetime.utcnow()
            await db.commit()
            if "Flood" in reason:
                break
        finally:
            if wrapper is not None:
                await pool.release(wrapper)
    counts["status"] = (
        "partial"
        if counts["failed"] and counts["updated"]
        else ("failed" if counts["failed"] else ("updated" if counts["updated"] else "skipped"))
    )
    if not rows:
        counts["reason"] = "no_stale_groups_due"
    return counts
