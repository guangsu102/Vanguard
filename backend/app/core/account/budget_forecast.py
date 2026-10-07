"""Isolate optional read-budget forecasts from the enclosing business transaction."""

from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import datetime
from typing import Any

import structlog

logger = structlog.get_logger()
Forecast = Callable[[Any, int, datetime, dict, dict], Awaitable[None]]


async def apply_forecast(
    db: Any, account_id: int, now: datetime, stage: str,
    forecast: Forecast, limits: dict, budget: dict,
) -> None:
    # begin_nested flushes pending business changes. Keep that outside the
    # optional-error handler: a failed business flush must reach its caller.
    savepoint = await db.begin_nested()
    proposed_limits, proposed_budget = deepcopy(limits), deepcopy(budget)
    try:
        await forecast(db, account_id, now, proposed_limits, proposed_budget)
        await savepoint.commit()
    except Exception as exc:
        await savepoint.rollback()
        if not db.is_active:
            raise
        # Discard partial loans and preserve hard counters and reservations.
        budget.setdefault("forecast_errors", {})[stage] = type(exc).__name__
        logger.warning(
            "telegram_read_forecast_failed", account_id=account_id,
            stage=stage, error_type=type(exc).__name__, exc_info=True,
        )
        return
    limits.clear()
    limits.update(proposed_limits)
    budget.clear()
    budget.update(proposed_budget)
