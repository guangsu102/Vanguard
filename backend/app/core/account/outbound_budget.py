"""Durable, account-serialized outbound quotas; unknown writes never expire."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from math import ceil
from typing import Any

from sqlalchemy import and_, or_, select

from app.core.account.models import AccountOperationConfig, AccountOutboundAttempt, TelegramAccount
from app.core.operating_time import operating_day_start
from app.core.settings_models import SystemSetting

CATEGORIES = {"ad", "verification", "diagnostic", "other"}
TERMINAL = {"succeeded", "failed", "cancelled"}
LEASE_SECONDS = 300
OFFICIAL_SPAMBOT_TARGET_KEY = "178220800"


class OutboundBudgetBlocked(RuntimeError):
    def __init__(self, reason: str, retry_after_seconds: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds


def _value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def account_capacity_limits(account: Any, config: Any, now: datetime) -> dict[str, Any]:
    blocked = (
        account is None
        or config is None
        or not getattr(account, "is_active", False)
        or not getattr(config, "enabled", False)
        or _value(getattr(account, "status", None)) in {"restricted", "banned", "error"}
        or _value(getattr(account, "risk_level", None)) not in {"normal", "watch"}
        or bool(getattr(account, "risk_pause_until", None) and account.risk_pause_until > now)
    )
    cautious = bool(
        account
        and (
            _value(getattr(account, "risk_level", None)) == "watch"
            or (getattr(account, "risk_recovery_until", None) and account.risk_recovery_until > now)
        )
    )
    diagnostic_only = bool(
        account is not None
        and config is not None
        and getattr(account, "is_active", False)
        and _value(getattr(account, "status", None)) == "restricted"
    )
    ceiling = 0 if blocked else 10 if cautious else 30
    configured_total = getattr(config, "max_messages_per_day", None)
    total = max(0, min(62, int(62 if configured_total is None else configured_total)))
    adaptive = getattr(config, "adaptive_ads_enabled", False) is True
    pace = max(1800 if cautious else 600, int(getattr(config, "message_interval_seconds", 0) or 0))
    probe = min(ceiling, max(0, int(getattr(config, "max_ads_per_day", 30))))
    ad_limit = (0 if blocked or probe == 0 else 86400 // pace) if adaptive else probe
    if adaptive and not blocked:
        total = ad_limit + min(30, max(0, int(config.max_verification_messages_per_day))) + min(2, max(0, int(config.max_diagnostic_messages_per_day)))
    return {
        "adaptive_ads": adaptive,
        "ad_probe": probe,
        "state": "blocked" if blocked else "recovery" if cautious else "normal",
        "total": 0 if blocked and not diagnostic_only else total,
        "ad": ad_limit,
        "verification": 0
        if blocked
        else min(30, max(0, int(getattr(config, "max_verification_messages_per_day", 30)))),
        "diagnostic": 0
        if blocked and not diagnostic_only
        else min(2, max(0, int(getattr(config, "max_diagnostic_messages_per_day", 2)))),
        "other": 0,
        "join": min(ceiling, max(0, int(getattr(config, "max_groups_per_day", 30)))),
        "join_interval_seconds": 7200 if cautious else 2880,
        "ad_interval_seconds": max(
            1800 if cautious else 600, int(getattr(config, "message_interval_seconds", 0) or 0)
        ),
    }


FEEDBACK_ACTIONS = {
    "join": "join",
    "ad_delivery": "ad",
    "verification_answer": "verification",
    "verification_callback": "verification",
    "spam_check": "diagnostic",
    "search": "search",
}


def _feedback_time(value: Any) -> datetime | None:
    from app.core.account.age import utc_naive

    return utc_naive(value) if value else None


class ActionCapacityFeedbackService:
    """Durable per-action pacing, isolated from account-wide health sanctions."""

    def __init__(self, db: Any):
        self.db = db

    @staticmethod
    def key(account_id: int) -> str:
        return f"automation.capacity_feedback.account.{account_id}"

    async def read(self, account_id: int) -> dict[str, Any]:
        row = await self.db.get(SystemSetting, self.key(account_id), populate_existing=True)
        try:
            data = json.loads(row.value) if row else {}
        except (TypeError, ValueError) as exc:
            raise OutboundBudgetBlocked("capacity_feedback_invalid") from exc
        if not isinstance(data, dict):
            raise OutboundBudgetBlocked("capacity_feedback_invalid")
        return data

    async def _locked(
        self, account_id: int
    ) -> tuple[
        TelegramAccount | None, AccountOperationConfig | None, SystemSetting | None, dict[str, Any]
    ]:
        account, config = await AccountOutboundBudgetService(self.db)._account(
            account_id, lock=True
        )
        row = await self.db.scalar(
            select(SystemSetting).where(SystemSetting.key == self.key(account_id)).with_for_update()
        )
        return account, config, row, await self.read(account_id)

    @staticmethod
    def _normal(config: Any, action: str) -> tuple[int, int]:
        if action == "join":
            return min(30, max(0, config.max_groups_per_day)), max(
                2880, config.join_interval_min_seconds or 0
            )
        if action == "ad_delivery":
            interval = max(600, config.message_interval_seconds or 0)
            limit = 86400 // interval if getattr(config, "adaptive_ads_enabled", False) is True else min(30, max(0, config.max_ads_per_day))
            return limit, interval
        if action.startswith("verification_"):
            return min(30, max(0, config.max_verification_messages_per_day)), 0
        if action == "spam_check":
            return min(2, max(0, config.max_diagnostic_messages_per_day)), 0
        return 100, 30

    async def record_flood(
        self,
        account_id: int,
        action: str,
        wait_seconds: int,
        *,
        now: datetime | None = None,
        event_key: str | None = None,
    ) -> None:
        now = now or datetime.utcnow()
        account, config, row, data = await self._locked(account_id)
        if not getattr(config, "dynamic_capacity_enabled", False) or action not in FEEDBACK_ACTIONS:
            return
        normal_limit, normal_interval = self._normal(config, action)
        health = account_capacity_limits(account, config, now)
        category = FEEDBACK_ACTIONS[action]
        effective_limit = min(normal_limit, health.get(category, normal_limit))
        effective_interval = max(normal_interval, health.get(category + "_interval_seconds", 0))
        previous = data.get(action) or {}
        if event_key and previous.get("last_event_key") == event_key:
            await self.db.commit()
            return
        effective_limit = min(effective_limit, int(previous.get("limit", effective_limit)))
        effective_interval = max(
            effective_interval, int(previous.get("interval_seconds", effective_interval))
        )
        old_pause = _feedback_time(previous.get("pause_until"))
        pause = max(now + timedelta(seconds=max(0, int(wait_seconds)) + 60), old_pause or now)
        data[action] = {
            "limit": max(1, effective_limit // 2) if effective_limit else 0,
            "interval_seconds": max(2, effective_interval * 2),
            "last_flood_at": now.isoformat(),
            "pause_until": pause.isoformat(),
            "last_success_at": None,
            "last_recovery_at": None,
            "last_event_key": event_key,
        }
        if row is None:
            row = SystemSetting(
                key=self.key(account_id), description="Per-action Telegram rate feedback"
            )
            self.db.add(row)
        row.value = json.dumps(data, sort_keys=True)
        await self.db.commit()

    async def record_success(
        self, account_id: int, action: str, *, now: datetime | None = None
    ) -> None:
        now = now or datetime.utcnow()
        if not (await self.read(account_id)).get(action):
            return
        _account, config, row, data = await self._locked(account_id)
        state = data.get(action)
        if not state or not getattr(config, "dynamic_capacity_enabled", False):
            return
        last_flood = _feedback_time(state.get("last_flood_at"))
        last_recovery = _feedback_time(state.get("last_recovery_at")) or last_flood
        pause = _feedback_time(state.get("pause_until"))
        state["last_success_at"] = now.isoformat()
        if (
            last_flood
            and last_recovery
            and now >= last_flood + timedelta(hours=24)
            and now >= last_recovery + timedelta(hours=24)
            and (pause is None or pause <= now)
        ):
            normal_limit, normal_interval = self._normal(config, action)
            state["limit"] = min(
                normal_limit, int(state["limit"]) + max(1, ceil(normal_limit * 0.2))
            )
            state["interval_seconds"] = max(
                normal_interval, ceil(normal_interval * normal_limit / max(1, state["limit"]))
            )
            state["last_recovery_at"] = now.isoformat()
        row.value = json.dumps(data, sort_keys=True)
        await self.db.commit()


async def effective_capacity_limits(
    db: Any, account: Any, config: Any, now: datetime
) -> dict[str, Any]:
    limits = account_capacity_limits(account, config, now)
    data = await ActionCapacityFeedbackService(db).read(account.id) if account is not None else {}
    limits["action_feedback"] = data
    limits["action_pauses"] = {}
    for action, category in FEEDBACK_ACTIONS.items():
        state = data.get(action)
        if not state or action in {"search", "verification_callback"}:
            continue
        limits[category] = min(limits.get(category, 0), max(0, int(state["limit"])))
        key = category + "_interval_seconds"
        limits[key] = max(limits.get(key, 0), max(0, int(state["interval_seconds"])))
        pause = _feedback_time(state.get("pause_until"))
        if pause and pause > now:
            limits["action_pauses"][category] = pause.isoformat()
    if limits.get("adaptive_ads"):
        limits["ad"] = min(limits["ad"], 86400 // max(600, limits["ad_interval_seconds"]))
        limits["ad_probe"] = min(limits["ad_probe"], limits["ad"])
        limits["total"] = limits["ad"] + limits["verification"] + limits["diagnostic"]
    return limits


def ad_lane(row: Any) -> str:
    from app.modules.acquisition.adaptive_frequency import payload
    return "mature" if payload(getattr(row, "context_json", None)).get("frequency", {}).get("lane") == "mature" else "probe"


class AccountOutboundBudgetService:
    def __init__(self, db: Any):
        self.db = db

    async def _account(
        self, account_id: int, *, lock: bool = False
    ) -> tuple[TelegramAccount | None, AccountOperationConfig | None]:
        if lock:
            await self.db.flush()
        q = select(TelegramAccount).where(TelegramAccount.id == account_id)
        c = select(AccountOperationConfig).where(AccountOperationConfig.account_id == account_id)
        if lock:
            q, c = (
                q.with_for_update(of=TelegramAccount),
                c.with_for_update(of=AccountOperationConfig),
            )
        return (
            await self.db.scalar(q.execution_options(populate_existing=True)),
            await self.db.scalar(c.execution_options(populate_existing=True)),
        )

    async def _attempts(self, account_id: int, now: datetime) -> list[AccountOutboundAttempt]:
        return list(
            (
                await self.db.scalars(
                    select(AccountOutboundAttempt).where(
                        AccountOutboundAttempt.account_id == account_id,
                        or_(
                            AccountOutboundAttempt.state.in_(["attempted", "unknown"]),
                            and_(
                                AccountOutboundAttempt.state == "reserved",
                                AccountOutboundAttempt.lease_expires_at > now,
                            ),
                            AccountOutboundAttempt.attempted_at >= now - timedelta(hours=24),
                        ),
                    )
                )
            ).all()
        )

    @staticmethod
    def _counted(row: Any, since: datetime, now: datetime) -> bool:
        if row.state in {"unknown", "attempted"}:
            return True  # Reconciliation owns release, never a clock or a lease expiry.
        if row.state == "reserved":
            return bool(row.lease_expires_at and row.lease_expires_at > now)
        return row.state in {"succeeded", "failed"} and bool(
            row.attempted_at and row.attempted_at >= since
        )

    async def snapshot(
        self, account_id: int, now: datetime | None = None, *, exclude_key: str | None = None
    ) -> dict[str, Any]:
        now = now or datetime.utcnow()
        account, config = await self._account(account_id)
        if config is None or not getattr(config, "dynamic_capacity_enabled", False):
            return {
                "account_id": account_id,
                "enabled": False,
                "blockers": ["dynamic_capacity_disabled"],
                "remaining": 0,
            }
        limits = await effective_capacity_limits(self.db, account, config, now)
        rows = [r for r in await self._attempts(account_id, now) if r.attempt_key != exclude_key]
        if limits.get("adaptive_ads"):
            rows = [r for r in rows if not (r.category == "ad" and r.state == "failed")]
        day, rolling = operating_day_start(now), now - timedelta(hours=24)
        today = [r for r in rows if self._counted(r, day, now)]
        window = [r for r in rows if self._counted(r, rolling, now)]
        categories = {}
        for category in sorted(CATEGORIES):
            a, b = (
                sum(r.category == category for r in today),
                sum(r.category == category for r in window),
            )
            categories[category] = {
                "effective": limits[category],
                "used_today": a,
                "used_rolling_24h": b,
                "remaining": max(0, min(limits[category] - a, limits[category] - b)),
            }
        ad_lanes = {}
        for lane in ("probe", "mature"):
            a = sum(r.category == "ad" and ad_lane(r) == lane and r.state != "failed" for r in today)
            b = sum(r.category == "ad" and ad_lane(r) == lane and r.state != "failed" for r in window)
            cap = limits.get("ad_probe", limits["ad"]) if lane == "probe" else limits["ad"]
            ad_lanes[lane] = {"effective": cap, "used_today": a, "used_rolling_24h": b,
                              "remaining": max(0, min(cap - a, cap - b, categories["ad"]["remaining"]))}
        last = max(
            (r.attempted_at for r in rows if r.category == "ad" and r.attempted_at), default=None
        )
        due = last + timedelta(seconds=limits["ad_interval_seconds"]) if last else None
        ad_pause = _feedback_time(limits["action_pauses"].get("ad"))
        due = max(filter(None, (due, ad_pause)), default=None)
        for category in CATEGORIES:
            latest = max(
                (r.attempted_at for r in rows if r.category == category and r.attempted_at),
                default=None,
            )
            next_at = (
                latest + timedelta(seconds=limits.get(category + "_interval_seconds", 0))
                if latest
                else None
            )
            next_at = max(
                filter(None, (next_at, _feedback_time(limits["action_pauses"].get(category)))),
                default=None,
            )
            categories[category]["next_allowed_at"] = (
                next_at.isoformat() if next_at and next_at > now else None
            )
        remaining = max(0, min(limits["total"] - len(today), limits["total"] - len(window)))
        blockers = []
        if limits["state"] == "blocked":
            blockers.append("account_capacity_unavailable")
        if remaining <= 0:
            blockers.append("outbound_total_budget")
        return {
            "account_id": account_id,
            "enabled": True,
            "configured": int(
                62 if config.max_messages_per_day is None else config.max_messages_per_day
            ),
            "effective": limits["total"],
            "used_today": len(today),
            "used_rolling_24h": len(window),
            "remaining": remaining,
            "next_allowed_at": due.isoformat() if due and due > now else None,
            "blockers": blockers,
            "categories": categories,
            "ad_lanes": ad_lanes if limits.get("adaptive_ads") else None,
            "limits": limits,
            "unknown_count": sum(r.state in {"attempted", "unknown"} for r in rows),
        }

    async def _find(self, attempt_key: str, account_id: int) -> AccountOutboundAttempt | None:
        row = await self.db.scalar(
            select(AccountOutboundAttempt)
            .where(AccountOutboundAttempt.attempt_key == attempt_key)
            .with_for_update()
        )
        if row is not None and row.account_id != account_id:
            raise OutboundBudgetBlocked("outbound_idempotency_conflict")
        return row

    async def _assert_target_resolved(
        self, account_id: int, category: str, target_key: str, *, exclude_key: str
    ) -> None:
        unresolved = await self.db.scalar(
            select(AccountOutboundAttempt.id)
            .where(
                AccountOutboundAttempt.account_id == account_id,
                AccountOutboundAttempt.category == category,
                AccountOutboundAttempt.target_key == target_key,
                AccountOutboundAttempt.attempt_key != exclude_key,
                AccountOutboundAttempt.state.in_(["attempted", "unknown"]),
            )
            .limit(1)
        )
        if unresolved is not None:
            raise OutboundBudgetBlocked("outbound_target_reconciliation_required")

    async def _check(
        self, account_id: int, category: str, now: datetime, *, exclude_key: str | None = None,
        context: dict | None = None,
    ) -> None:
        account, config = await self._account(account_id)
        if category in {"ad", "verification"}:
            from app.core.account.age import account_age_eligibility_reason

            age_reason = account_age_eligibility_reason(account, now)
            if age_reason:
                raise OutboundBudgetBlocked(age_reason)
        if category == "ad" and not getattr(config, "auto_ads_enabled", False):
            raise OutboundBudgetBlocked("account_auto_ads_disabled")
        info = await self.snapshot(account_id, now, exclude_key=exclude_key)
        if not info.get("enabled"):
            raise OutboundBudgetBlocked("dynamic_capacity_disabled")
        blockers = [
            reason
            for reason in info["blockers"]
            if not (
                category == "diagnostic"
                and info["limits"]["diagnostic"] > 0
                and reason == "account_capacity_unavailable"
            )
        ]
        if blockers:
            raise OutboundBudgetBlocked(blockers[0])
        if category == "ad" and info["limits"].get("adaptive_ads"):
            from app.modules.acquisition.adaptive_frequency import FrequencyService
            fc = (context or {}).get("frequency") or {}
            if fc.get("lane") not in {"probe", "mature"} or not fc.get("telegram_group_id"):
                raise OutboundBudgetBlocked("frequency_reservation_required")
            state = await FrequencyService(self.db).state(fc["telegram_group_id"], lock=True)
            if (state is None or state.status != "active" or state.epoch != fc.get("epoch")
                    or state.quota != fc.get("quota") or state.mature != (fc["lane"] == "mature")
                    or (state.pause_until and state.pause_until > now)):
                raise OutboundBudgetBlocked("frequency_reservation_stale")
            if info["ad_lanes"][fc["lane"]]["remaining"] <= 0:
                raise OutboundBudgetBlocked("outbound_ad_" + fc["lane"] + "_budget")
        if info["categories"][category]["remaining"] <= 0:
            raise OutboundBudgetBlocked("outbound_" + category + "_budget")
        category_due = info["categories"][category].get("next_allowed_at")
        if category_due and category != "ad":
            due = datetime.fromisoformat(category_due)
            raise OutboundBudgetBlocked(
                "outbound_" + category + "_interval", max(1, ceil((due - now).total_seconds()))
            )
        if category == "ad":
            if info["next_allowed_at"]:
                due = datetime.fromisoformat(info["next_allowed_at"])
                raise OutboundBudgetBlocked(
                    "outbound_ad_interval", max(1, int((due - now).total_seconds()))
                )
            other = [
                r
                for r in await self._attempts(account_id, now)
                if r.attempt_key != exclude_key
                and r.category == "ad"
                and r.state == "reserved"
                and r.lease_expires_at
                and r.lease_expires_at > now
            ]
            if other:
                raise OutboundBudgetBlocked("outbound_ad_reconciliation_required")

    async def _frequency_receipt(self, account_id: int, key: str, target: str, context: dict, now: datetime) -> None:
        from app.modules.acquisition.adaptive_frequency import FrequencyService, canonical, enabled, frequency_context, payload
        from app.modules.acquisition.models import AdDeliveryLog
        if not await enabled(self.db, account_id):
            return
        if not key.startswith("ad:"):
            raise OutboundBudgetBlocked("frequency_reservation_required")
        token = key[3:]
        log = await self.db.scalar(select(AdDeliveryLog).where(
            AdDeliveryLog.reservation_token == token, AdDeliveryLog.account_id == account_id,
        ).execution_options(populate_existing=True))
        fc = (context or {}).get("frequency")
        if (log is None or log.status != "pending" or log.telegram_message_id is not None
                or not fc or frequency_context(log) != fc
                or canonical(target, payload(log.qualification_context_json)) != fc.get("telegram_group_id")):
            raise OutboundBudgetBlocked("frequency_reservation_mismatch")
        ready, _ = await FrequencyService(self.db).readiness(
            log.telegram_group_id, now, context=payload(log.qualification_context_json),
            reservation_token=token, lock=True,
        )
        if ready.reason:
            raise OutboundBudgetBlocked(ready.reason)

    async def reserve(
        self,
        account_id: int,
        *,
        attempt_key: str,
        category: str,
        target_key: str,
        context: dict | None = None,
        now: datetime | None = None,
    ) -> AccountOutboundAttempt:
        now = now or datetime.utcnow()
        if (
            category not in CATEGORIES
            or not 1 <= len(attempt_key) <= 128
            or not 1 <= len(target_key) <= 128
        ):
            raise OutboundBudgetBlocked("outbound_invalid_request")
        if category == "diagnostic" and target_key != OFFICIAL_SPAMBOT_TARGET_KEY:
            raise OutboundBudgetBlocked("official_diagnostic_target_required")
        await self._account(account_id, lock=True)
        row = await self._find(attempt_key, account_id)
        if category == "ad" and (row is None or row.state == "reserved"):
            await self._frequency_receipt(account_id, attempt_key, target_key, context or {}, now)
        encoded = json.dumps(context or {}, sort_keys=True, separators=(",", ":"))
        if row is not None:
            if (
                row.category != category
                or row.target_key != target_key
                or row.context_json != encoded
            ):
                raise OutboundBudgetBlocked("outbound_idempotency_conflict")
            renewable = row.attempted_at is None and (
                row.state == "cancelled"
                or (
                    row.state == "reserved"
                    and row.lease_expires_at is not None
                    and row.lease_expires_at <= now
                )
            )
            if renewable:
                await self._check(account_id, category, now, exclude_key=attempt_key, context=context)
                await self._assert_target_resolved(
                    account_id, category, target_key, exclude_key=attempt_key
                )
                row.state, row.lease_expires_at = "reserved", now + timedelta(seconds=LEASE_SECONDS)
                row.completed_at, row.error_code, row.message_id = None, None, None
            await self.db.commit()
            return row  # Caller must never invoke RPC for a non-reserved attempt.
        await self._check(account_id, category, now, context=context)
        await self._assert_target_resolved(
            account_id, category, target_key, exclude_key=attempt_key
        )
        row = AccountOutboundAttempt(
            account_id=account_id,
            attempt_key=attempt_key,
            category=category,
            target_key=target_key,
            state="reserved",
            created_at=now,
            lease_expires_at=now + timedelta(seconds=LEASE_SECONDS),
            context_json=encoded,
        )
        self.db.add(row)
        await self.db.commit()
        return row

    async def mark_attempted(
        self, attempt_key: str, *, account_id: int, now: datetime | None = None
    ) -> AccountOutboundAttempt:
        now = now or datetime.utcnow()
        await self._account(account_id, lock=True)
        row = await self._find(attempt_key, account_id)
        if row is None or row.state != "reserved":
            raise OutboundBudgetBlocked("outbound_attempt_not_reserved")
        if not row.lease_expires_at or row.lease_expires_at <= now:
            raise OutboundBudgetBlocked("outbound_reservation_expired")
        if row.category == "ad":
            await self._frequency_receipt(account_id, attempt_key, row.target_key, json.loads(row.context_json or "{}"), now)
        await self._check(account_id, row.category, now, exclude_key=attempt_key, context=json.loads(row.context_json or "{}"))
        await self._assert_target_resolved(
            account_id, row.category, row.target_key, exclude_key=attempt_key
        )
        row.state, row.attempted_at, row.lease_expires_at = "attempted", now, None
        await self.db.commit()
        return row

    async def finish(
        self,
        attempt_key: str,
        *,
        account_id: int,
        state: str,
        message_id: int | None = None,
        error_code: str | None = None,
        now: datetime | None = None,
        reconciliation_confirmed: bool = False,
    ) -> AccountOutboundAttempt:
        now = now or datetime.utcnow()
        if state not in TERMINAL | {"unknown"}:
            raise OutboundBudgetBlocked("outbound_invalid_state")
        await self._account(account_id, lock=True)
        row = await self._find(attempt_key, account_id)
        if row is None:
            raise OutboundBudgetBlocked("outbound_attempt_missing")
        if row.state == state and (message_id is None or row.message_id == message_id):
            await self.db.commit()
            return row
        if row.state in TERMINAL:
            raise OutboundBudgetBlocked("outbound_attempt_terminal")
        if row.state == "unknown" and not reconciliation_confirmed:
            raise OutboundBudgetBlocked("outbound_reconciliation_required")
        if state == "cancelled" and row.attempted_at is not None:
            raise OutboundBudgetBlocked("outbound_attempt_already_sent")
        if state in {"succeeded", "unknown"} and row.attempted_at is None:
            raise OutboundBudgetBlocked("outbound_attempt_not_sent")
        if state == "succeeded" and (type(message_id) is not int or message_id <= 0):
            raise OutboundBudgetBlocked("outbound_message_id_required")
        row.state, row.message_id, row.error_code = (
            state,
            message_id,
            (error_code or "")[:160] or None,
        )
        row.completed_at, row.lease_expires_at = now, None
        await self.db.commit()
        return row

    async def reconcile(
        self,
        attempt_key: str,
        *,
        account_id: int,
        state: str,
        message_id: int | None = None,
        error_code: str | None = None,
        confirmed: bool = False,
        now: datetime | None = None,
    ) -> AccountOutboundAttempt:
        if not confirmed or state not in {"succeeded", "failed"}:
            raise OutboundBudgetBlocked("outbound_reconciliation_unconfirmed")
        return await self.finish(
            attempt_key,
            account_id=account_id,
            state=state,
            message_id=message_id,
            error_code=error_code,
            now=now,
            reconciliation_confirmed=True,
        )
