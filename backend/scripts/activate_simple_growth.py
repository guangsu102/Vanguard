"""Idempotent cutover after old growth consumers drain; never sends Telegram RPCs.

Run without --apply for a read-only census. Apply the Alembic expansion first.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.core.account.models import AccountOperationConfig, TelegramAccount
from app.core.database import get_db_session
from app.core.group.models import GroupAccountMembership as Member
from app.core.redis import get_redis
from app.core.settings_models import SystemSetting
from app.modules.acquisition.models import (
    AdDeliveryLog,
    AutoJoinAttempt,
    GroupAdFrequency,
    GroupQualificationAudit,
)
from app.modules.acquisition.simple_limits import PREFIX

MARKER = "growth.writable_v2.activation"
SEED = """
local day,used=tonumber(ARGV[1]),tonumber(ARGV[2])
if tonumber(redis.call('HGET',KEYS[1],'day') or '-1')==day then
 used=math.max(used,tonumber(redis.call('HGET',KEYS[1],'used') or '0'))
end
redis.call('HSET',KEYS[1],'day',day,'used',used)
redis.call('EXPIREAT',KEYS[1],(day+2)*86400)
local due=math.max(tonumber(ARGV[3]),tonumber(redis.call('GET',KEYS[2]) or '0'))
if due>tonumber(ARGV[4]) then redis.call('SET',KEYS[2],due,'EXAT',math.ceil(due)) end
return used
"""
MAX_VALUE = """
local value=math.max(tonumber(ARGV[1]),tonumber(redis.call('GET',KEYS[1]) or '0'))
redis.call('SET',KEYS[1],value,'EX',ARGV[2]);return value
"""


def convert_member(member: Member, now: datetime, permissions: dict | None = None) -> bool:
    """Return whether send history needs a cooldown projection; never grant permission."""
    permissions = permissions or {}
    verification_pending = (permissions.get("verification_pending") is True
                            or permissions.get("verification_required") is True
                            or member.review_status in {"verification_pending", "verification_required"})
    paused = member.ad_status == "paused"
    owned = member.review_status == "owned_group_excluded"
    real_exit = member.leave_error in {"permanent_mute", "temporary_mute", "deleted_at_minimum"}
    member.can_send_text, member.permission_checked_at = None, None
    if paused or owned:
        member.review_status, member.ad_status = "rejected", "paused" if paused else "blocked"
        member.permission_reason = "manual_pause" if paused else "owned_group_protected"
        return False
    if member.status not in {"joined", "leave_failed"}:
        member.review_status = "pending" if member.status == "pending" else "rejected"
        member.ad_status = "blocked"
        return False
    if member.status == "leave_failed" and not real_exit:
        member.status = "joined"  # The old rule-driven leave failed; recheck actual membership.
    member.review_status, member.ad_status = "pending", "blocked"
    member.review_next_at, member.permission_reason = now, "cutover_permission_check"
    member.verification_status = "pending" if verification_pending else "unknown"
    if not real_exit:
        member.leave_requested_at = member.leave_retry_at = member.leave_error = None
    return True


async def activate(apply: bool = False) -> dict:
    # Register relationship targets without booting workers or creating production tables.
    for module in ("app.core.keyword.models", "app.core.user.models", "app.core.campaign.models",
                   "app.modules.guardian.models", "app.modules.owned_group.models",
                   "app.modules.owned_group.models_extra", "app.modules.owned_group.messaging_models",
                   "app.modules.acquisition.qualification_snapshot", "app.modules.acquisition.p2_models",
                   "app.modules.acquisition.p4_models", "app.modules.private_chat.models"):
        importlib.import_module(module)
    now = datetime.now(UTC).replace(tzinfo=None)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    from app.core.operating_time import operating_day_start
    ad_day_start = operating_day_start(now)
    async with get_db_session() as db:
        counts = dict((await db.execute(select(Member.review_status, func.count(Member.id)).group_by(Member.review_status))).all())
        if not apply:
            return {"apply": False, "memberships": counts}
        marker = await db.get(SystemSetting, MARKER)
        progress = json.loads(marker.value) if marker else {}
        if not progress.get("converted_at"):
            # Consumers are stopped during this bounded, local-only conversion.
            cursor = int(progress.get("cursor", 0))
            while True:
                rows = list((await db.scalars(select(Member).where(Member.id > cursor).order_by(Member.id).limit(200))).all())
                if not rows:
                    break
                for member in rows:
                    cursor = member.id
                    evidence = await db.scalar(select(GroupQualificationAudit.evidence_json).where(
                        GroupQualificationAudit.membership_id == member.id,
                        GroupQualificationAudit.membership_joined_at == member.joined_at,
                    ).order_by(GroupQualificationAudit.id.desc()).limit(1))
                    try:
                        permissions = json.loads(evidence or "{}").get("permissions", {})
                    except (ValueError, AttributeError):
                        permissions = {}
                    needs_cooldown = convert_member(member, now, permissions)
                    # A manually paused member can be resumed immediately after
                    # cutover. Its unknown historical send still needs the same
                    # one-time cooldown; this never changes the manual pause.
                    if not needs_cooldown and member.status not in {"joined", "leave_failed"}:
                        continue
                    last = await db.scalar(select(func.max(AdDeliveryLog.sent_at)).where(
                        AdDeliveryLog.group_id == member.group_id, AdDeliveryLog.status == "success"))
                    uncertain = await db.scalar(select(AdDeliveryLog.id).where(
                        AdDeliveryLog.group_id == member.group_id,
                        AdDeliveryLog.status.in_(["pending", "sending", "unknown", "reconciliation_required", "send_unknown"]),
                    ).limit(1))
                    frequency = await db.get(GroupAdFrequency, member.telegram_group_id)
                    gap = timedelta(seconds=86400 // max(1, frequency.quota if frequency else 1))
                    member.next_ad_allowed_at = max(member.next_ad_allowed_at or now, now,
                        last + gap if last else now, now + gap if uncertain else now)
                if marker is None:
                    marker = SystemSetting(key=MARKER, value="{}")
                    db.add(marker)
                marker.value = json.dumps({"cursor": cursor})
                await db.commit()
            frequency_cursor = progress.get("frequency_cursor")
            while True:
                query = select(GroupAdFrequency).order_by(GroupAdFrequency.telegram_group_id).limit(200)
                if frequency_cursor is not None:
                    query = query.where(GroupAdFrequency.telegram_group_id > frequency_cursor)
                frequencies = list((await db.scalars(query)).all())
                if not frequencies:
                    break
                for frequency in frequencies:
                    frequency_cursor = frequency.telegram_group_id
                    frequency.quota = max((q for q in (1, 2, 4, 8, 16, 30) if q <= frequency.quota), default=1)
                    # Old deletion/AI evidence cannot authorize an exit in the new loop.
                    # Real membership restrictions remain blocked until a live recheck.
                    frequency.status, frequency.reason, frequency.mature = "active", "cutover_permission_check", True
                    frequency.daily_review_due_at = frequency.daily_review_log_id = None
                    frequency.daily_review_token = frequency.daily_review_expires_at = None
                    frequency.daily_review_retry_at = frequency.daily_review_error = None
                if marker is None:
                    marker = SystemSetting(key=MARKER, value="{}")
                    db.add(marker)
                marker.value = json.dumps({"cursor": cursor, "frequency_cursor": frequency_cursor})
                await db.commit()
            if marker is None:
                marker = SystemSetting(key=MARKER, value="{}")
                db.add(marker)
            marker.value = json.dumps({"converted_at": now.isoformat(), "cursor": cursor})
            await db.commit()
        configs = list((await db.scalars(select(AccountOperationConfig))).all())
        seeds = []
        for config in configs:
            join_count, last_join = (await db.execute(select(func.count(AutoJoinAttempt.id), func.max(AutoJoinAttempt.request_sent_at)).where(
                AutoJoinAttempt.account_id == config.account_id, AutoJoinAttempt.telegram_action_attempted.is_(True),
                AutoJoinAttempt.attempted_at >= day_start))).one()
            ad_count, last_ad = (await db.execute(select(func.count(AdDeliveryLog.id), func.max(AdDeliveryLog.sent_at)).where(
                AdDeliveryLog.account_id == config.account_id, AdDeliveryLog.created_at >= ad_day_start,
                AdDeliveryLog.status.in_(["success", "unknown", "failed", "sending", "permission_changed"])
            ))).one()
            account = await db.get(TelegramAccount, config.account_id)
            recovering = config.business_stage == "cooldown" or account.risk_level == "watch" or bool(
                account.risk_recovery_until and account.risk_recovery_until > now)
            join_due = max(now, config.next_join_after or now,
                           last_join + timedelta(seconds=7200 if recovering else 2880) if last_join else now)
            ad_due = max(now, last_ad + timedelta(seconds=max(600, config.message_interval_seconds)) if last_ad else now)
            seeds.append((config.account_id, join_count, join_due, ad_count, ad_due))
            config.join_review_backlog_paused = False
            config.join_review_backlog_reason = None
        group_seeds = (await db.execute(select(Member.telegram_group_id, func.max(Member.next_ad_allowed_at))
            .where(Member.next_ad_allowed_at > now).group_by(Member.telegram_group_id))).all()
        await db.commit()
    # No PostgreSQL transaction spans Redis I/O.
    redis = await get_redis()
    epoch = now.replace(tzinfo=UTC).timestamp()
    for account, joins, join_due, ads, ad_due in seeds:
        for kind, count, due in (("join", joins, join_due), ("ads", ads, ad_due)):
            await redis.eval(SEED, 2, PREFIX + f"{kind}:{account}:count", PREFIX + f"{kind}:{account}:due",
                             int(epoch + (28800 if kind == "ads" else 0)) // 86400,
                             count, int(due.replace(tzinfo=UTC).timestamp()), int(epoch))
    from app.modules.acquisition.adaptive_frequency import canonical
    for target, due in group_seeds:
        key = canonical(target)
        if key is not None:
            value = int(due.replace(tzinfo=UTC).timestamp())
            await redis.eval(MAX_VALUE, 1, PREFIX + f"group:{key}:due", value, max(1, value - int(epoch)))
    return {"apply": True, "memberships_before": counts, "accounts_seeded": len(seeds)}


async def main(apply: bool) -> dict:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core import database
    from app.core.config import settings
    from app.core.redis import init_redis
    engine = create_async_engine(settings.DATABASE_URL, pool_size=2, max_overflow=1,
                                 pool_pre_ping=True)
    database.async_session_factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        if apply:
            await init_redis()
        return await activate(apply)
    finally:
        if apply:
            await (await get_redis()).aclose()
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    print(json.dumps(asyncio.run(main(parser.parse_args().apply)), ensure_ascii=False))
