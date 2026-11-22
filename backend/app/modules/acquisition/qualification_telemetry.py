"""Optional bounded read-only runtime projection; owns no business session."""
from __future__ import annotations

import asyncio
from datetime import datetime

REPORT_TIMEOUT = .4


def _enabled() -> bool:
    from app.modules.acquisition.qualification_sla_lifecycle import enabled
    return enabled()


def _engine_enabled(account_id: int) -> bool:
    from app.modules.acquisition.qualification_engine import enabled_account_ids
    return account_id in enabled_account_ids()


def _session_factory():
    from app.core import database
    return database.async_session_factory


def _load_reports():
    from app.modules.acquisition.qualification_sla import report
    from app.modules.acquisition.qualification_cost import cost_report_summary
    return report, cost_report_summary


def _unknown(reason: str) -> dict:
    return {"startup": {"available": False, "status": "not_evaluable", "reason": reason,
        "strict_rate": None, "guaranteed_lower_bound": None,
        "eligible_mature": None, "potential_mature_never_started": None},
        "cost": {"available": False, "coverage_complete": False, "reason": reason,
            "tasks": [], "total_tasks": None, "returned_tasks": 0, "tasks_limit": 20,
            "tasks_truncated": False, "aggregate": None,
            "matched_task_savings": None, "net_savings": None}}


async def snapshot(account_id: int, now: datetime, *, sdk_coverage: dict | None = None,
                   since: datetime | None = None) -> dict:
    """Return startup/cost summaries, leaving existing runtime fields untouched.

    Resolving a session from the existing factory creates no engine/pool. The
    separate read transaction is closed on timeout or error; the API caller's
    transaction is never flushed, committed, rolled back or cancelled.
    """
    result = _unknown("measurement_unavailable")
    try:
        if not _enabled():
            return _unknown("measurement_disabled")
        if type(account_id) is not int or not _engine_enabled(account_id):
            return _unknown("account_engine_disabled")
        factory = _session_factory()
        if factory is None:
            return _unknown("measurement_database_unavailable")
        sla_report, cost_report = _load_reports()
        async with asyncio.timeout(REPORT_TIMEOUT):
            async with factory() as db:
                if getattr(getattr(getattr(db, "bind", None), "dialect", None), "name", None) == "postgresql":
                    from sqlalchemy import text
                    await db.execute(text("SET TRANSACTION READ ONLY"))
                    await db.execute(text("SET LOCAL statement_timeout='200ms'"))
                result["startup"] = await sla_report(db, account_id, now, since=since)
                result["cost"] = await cost_report(db, account_id, now, since=since,
                    sdk_coverage=sdk_coverage, limit=20)
                result["cost"]["available"] = True
        return result
    except Exception as exc:
        reason = "measurement_timeout" if isinstance(exc, TimeoutError) else "measurement_unavailable"
        fallback = _unknown(reason)
        # Preserve an already completed independent projection. No missing
        # denominator or cost is filled with zero after a later report failure.
        if result["startup"].get("available") is True:
            fallback["startup"] = result["startup"]
        return fallback
