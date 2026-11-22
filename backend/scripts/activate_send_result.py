"""Preview/apply the send-result cutover. Archives are mandatory and mode 0600."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from app.core.account.models import AccountOperationConfig, AccountType, TelegramAccount, AccountOutboundAttempt
from app.core.database import get_db_session
from app.core.group.models import GroupAccountMembership as Member
from app.core.settings_models import SystemSetting
from app.core.worker_status import TelegramEventInbox as Inbox
from app.modules.acquisition.models import AdDeliveryLog, GroupAdFrequency
from app.modules.acquisition.send_result_policy import LEGACY_EXITS, POLICY

KEY = "growth.send_result.cutover.v1"


def record(row) -> dict:
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


def archive(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(data, out, ensure_ascii=False, default=str)


async def cutover(*, apply: bool = False, snapshot: Path | None = None, resume: bool = False) -> dict:
    async with get_db_session() as db:
        marker = await db.get(SystemSetting, KEY)
        if resume:
            if not marker:
                raise ValueError("cutover_not_applied")
            state = json.loads(marker.value)
            if not state.get("resumed_at"):
                for item in state["operations"]:
                    cfg = await db.get(AccountOperationConfig, item["id"])
                    # Only undo the temporary cutover pause, never turn on a previously disabled account.
                    if cfg and cfg.enabled is False:
                        cfg.enabled = item["enabled"]
                state["resumed_at"] = datetime.utcnow().isoformat()
                marker.value = json.dumps(state)
                await db.commit()
            return {"resumed": True}
        if marker:
            return {"already_applied": True, "state": json.loads(marker.value)}
        accounts = list((await db.scalars(select(TelegramAccount).where(
            TelegramAccount.account_type == AccountType.PROMOTER))).all())
        ids = [a.id for a in accounts]
        configs = list((await db.scalars(select(AccountOperationConfig).where(
            AccountOperationConfig.account_id.in_(ids)))).all())
        members = list((await db.scalars(select(Member).where(Member.account_id.in_(ids)))).all())
        logs = list((await db.scalars(select(AdDeliveryLog).where(AdDeliveryLog.account_id.in_(ids),
            AdDeliveryLog.survival_status.in_(["pending", "checking", "unknown"])))) .all())
        frequencies = list((await db.scalars(select(GroupAdFrequency).join(AdDeliveryLog,
            AdDeliveryLog.id == GroupAdFrequency.daily_review_log_id).where(AdDeliveryLog.account_id.in_(ids)))).all())
        events = list((await db.scalars(select(Inbox).where(Inbox.account_id.in_(ids),
            Inbox.state.in_(["pending", "processing", "executing", "reconciliation_required"]),
            Inbox.kind.in_(["message", "join"])))).all())
        now = datetime.utcnow()
        expired = [e for e in events if not e.external_attempted and e.received_at < now - timedelta(hours=1)]
        uncertain = [e for e in events if e.external_attempted and e.state == "reconciliation_required"]
        ledger = list((await db.scalars(select(AccountOutboundAttempt).where(
            AccountOutboundAttempt.account_id.in_(ids), AccountOutboundAttempt.category == "ad",
            AccountOutboundAttempt.state == "unknown"))).all())
        unknown_logs = list((await db.scalars(select(AdDeliveryLog).where(AdDeliveryLog.account_id.in_(ids),
            (AdDeliveryLog.status == "unknown") | AdDeliveryLog.qualification_context_json.contains("manual_evidence_required")))).all())
        canary_before = await db.get(SystemSetting, "growth.canary.account.2")
        result = {"unknown_ad_ledger": len(ledger), "unknown_ad_logs": len(unknown_logs), "accounts": ids, "old_reviews": len(logs), "frequency_targets": len(frequencies),
                  "expired_business_events": len(expired), "unknown_business_events": len(uncertain)}
        if not apply:
            return {"preview": result}
        if snapshot is None:
            raise ValueError("snapshot_required")
        operations = [record(c) for c in configs]
        pause = await db.get(SystemSetting, "growth.send_result.release_pause.v1")
        if pause:
            before = json.loads(pause.value).get("operations", [])
            for item in operations:
                saved = next((entry for entry in before if entry["id"] == item["id"]), None)
                if saved:
                    item["enabled"] = saved["enabled"]
        archive(snapshot, {"at": now.isoformat(), "counts": result, "operations": operations,
            "members": [record(m) for m in members], "logs": [record(l) for l in logs],
            "frequencies": [record(f) for f in frequencies], "events": [record(e) for e in expired + uncertain],
            "unknown_ad_ledger": [record(l) for l in ledger], "unknown_ad_logs": [record(l) for l in unknown_logs],
            "canary_before": record(canary_before) if canary_before else None})
        for item in ledger:
            context = json.loads(item.context_json or "{}")
            context.update(archived_under_send_result=now.isoformat(), automatic_resend=False)
            item.context_json = json.dumps(context)
        for cfg in configs:
            cfg.enabled = False
        for row in logs:
            row.survival_status, row.survival_stage = "not_required", "complete"
            row.survival_error, row.survival_check_due_at = "policy_disabled_send_result", None
        for row in frequencies:
            row.daily_review_due_at = row.daily_review_log_id = row.daily_review_token = None
            row.daily_review_expires_at = row.daily_review_retry_at = None
            row.daily_review_error = "policy_disabled_send_result"
        from app.core.automation_settings import get_ad_delivery_execution_settings
        from app.modules.acquisition.models import AdCampaign
        settings = await get_ad_delivery_execution_settings(db)
        for member in members:
            member.ad_failure_streak = 0
            if member.status == "left":
                continue
            if member.leave_error in LEGACY_EXITS:
                member.leave_requested_at = member.leave_retry_at = member.leave_error = None
                member.leave_attempts = 0
                if member.status == "leave_failed":
                    member.status = "joined"
            if member.ad_status == "paused" or member.leave_requested_at or member.status != "joined":
                continue
            # A known challenge stays blocked. Completed verification needs no daily reread.
            if member.verification_status == "complete":
                member.review_status, member.ad_status, member.can_send_text = "approved", "active", True
                member.review_next_at = None
                last = await db.scalar(select(AdDeliveryLog).where(AdDeliveryLog.account_id == member.account_id,
                    AdDeliveryLog.group_id == member.group_id, AdDeliveryLog.status.in_(["success", "unknown", "sending"])
                    ).order_by(AdDeliveryLog.created_at.desc(), AdDeliveryLog.id.desc()).limit(1))
                campaign = await db.get(AdCampaign, last.ad_campaign_id) if last and last.ad_campaign_id else None
                gap = max(settings["growth_group_global_cooldown_seconds"], (campaign.interval_minutes or 0) * 60 if campaign else 0)
                member.next_ad_allowed_at = max(now, last.created_at + timedelta(seconds=gap)) if last else now
        for event in expired + uncertain:
            event.state = "archived_unknown" if event.external_attempted else "expired"
            event.completed_at = now
            event.last_error = "cutover_unknown_no_replay" if event.external_attempted else "expired_no_replay"
            event.claim_owner = event.claim_lease_expires_at = None
            # Preserve payload, external_attempted and original outcome; do not call a business handler.
        state = {"policy": POLICY, "started_at": now.isoformat(), "operations": operations,
                 "snapshot": str(snapshot), "counts": result}
        account2 = next((a for a in accounts if a.id == 2), None)
        if account2:
            state["observed_risk_reason"] = account2.risk_reason
        canary = await db.get(SystemSetting, "growth.canary.account.2")
        if canary:
            value = json.loads(canary.value)
            value.update(enabled=True, started_at=now.isoformat(), send_result_started_at=now.isoformat(),
                         ad_daily=10, join_daily=10, ad_interval_seconds=1800, join_interval_seconds=7200)
            canary.value = json.dumps(value)
        db.add(SystemSetting(key=KEY, value=json.dumps(state, default=str), description="Send-result cutover audit"))
        await db.commit()
        return {"applied": result, "snapshot": str(snapshot)}


async def begin_observation(account_id: int, snapshot: Path, *, restart: bool = False) -> dict:
    if account_id not in {2, 33, 34, 35}:
        raise ValueError("account_not_in_recovery_cohort")
    async with get_db_session() as db:
        marker = await db.get(SystemSetting, KEY, with_for_update=True)
        if not marker:
            raise ValueError("cutover_required")
        account = await db.get(TelegramAccount, account_id)
        now = datetime.utcnow()
        if (account.spam_check_status != "clear" or not account.spam_checked_at
                or account.spam_checked_at < now - timedelta(hours=24)
                or account.status in {"restricted", "banned", "error"}
                or account.risk_level not in {"normal", "watch"}
                or account.risk_pause_until and account.risk_pause_until > now):
            raise ValueError("fresh_official_clear_required")
        rows = list((await db.scalars(select(SystemSetting).where(SystemSetting.key.like("growth.canary.account.%")))).all())
        already_observing = False
        for row in rows:
            policy = json.loads(row.value)
            if policy.get("enabled") and policy.get("send_result_started_at"):
                if row.key.endswith("." + str(account_id)):
                    already_observing = True
                else:
                    raise ValueError("another_account_observation_active")
        if already_observing and not restart:
            return {"already_observing": account_id}
        config = await db.scalar(select(AccountOperationConfig).where(AccountOperationConfig.account_id == account_id))
        if not config:
            raise ValueError("account_operation_config_required")
        key = f"growth.canary.account.{account_id}"
        row = await db.get(SystemSetting, key)
        archive(snapshot, {"account_id": account_id, "restart": restart, "operations": record(config),
                           "canary": record(row) if row else None, "observation": record(marker)})
        value = json.dumps({"enabled": True, "started_at": now.isoformat(), "send_result_started_at": now.isoformat(),
            "observed_risk_reason": account.risk_reason, "ad_daily": 10, "join_daily": 10,
            "ad_interval_seconds": 1800, "join_interval_seconds": 7200})
        if row:
            row.value = value
        else:
            db.add(SystemSetting(key=key, value=value))
        if not restart:
            config.enabled = config.auto_ads_enabled = config.auto_join_enabled = True
        await db.commit()
        return {"observing": account_id, "started_at": now.isoformat()}


async def manual_action(member_id: int, action: str, snapshot: Path) -> dict:
    from app.modules.acquisition.send_result_policy import EXIT_REASON
    async with get_db_session() as db:
        member = await db.get(Member, member_id, with_for_update=True)
        if not member or member.leave_error != EXIT_REASON or member.ad_status == "paused":
            raise ValueError("not_a_send_failure_exit")
        if action == "allow_rejoin" and member.status != "left":
            raise ValueError("confirmed_departure_required")
        archive(snapshot, {"action": action, "membership": record(member)})
        if action == "allow_rejoin":
            member.leave_requested_at = member.leave_retry_at = member.leave_error = None
            member.ad_failure_streak = 0
            # Keep status=left: only a real new join can create a usable relationship.
        else:
            member.leave_attempts = 0
            member.leave_retry_at = datetime.utcnow()
        await db.commit()
        return {"membership_id": member_id, "action": action}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--begin-observation", type=int)
    parser.add_argument("--restart-observation", action="store_true")
    parser.add_argument("--allow-rejoin", type=int)
    parser.add_argument("--retry-exit", type=int)
    parser.add_argument("--snapshot", type=Path)
    args = parser.parse_args()
    async def execute():
        import app.main  # Register every relationship without starting application lifespan.
        from app.core import database
        from app.core.config import settings
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        engine = create_async_engine(settings.DATABASE_URL, pool_size=2, max_overflow=1)
        database.async_session_factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        try:
            if args.allow_rejoin or args.retry_exit:
                if not args.snapshot:
                    raise ValueError("snapshot_required")
                return await manual_action(args.allow_rejoin or args.retry_exit, "allow_rejoin" if args.allow_rejoin else "retry_exit", args.snapshot)
            if args.begin_observation:
                if not args.snapshot:
                    raise ValueError("snapshot_required")
                return await begin_observation(args.begin_observation, args.snapshot, restart=args.restart_observation)
            if args.restart_observation:
                raise ValueError("begin_observation_required")
            return await cutover(apply=args.apply, snapshot=args.snapshot, resume=args.resume)
        finally:
            await engine.dispose()
    print(json.dumps(asyncio.run(execute()), default=str))


if __name__ == "__main__":
    main()
