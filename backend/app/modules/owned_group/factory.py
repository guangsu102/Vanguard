"""Construction helpers for the owned-group Telegram boundary.

The worker and HTTP control paths must use the same guarded adapter wiring.  A
single factory keeps account-pool, risk-guard, and Redis-safety construction in
one place while leaving imports lazy so importing the API never opens Telegram
clients or requires a running worker.
"""

from __future__ import annotations

from typing import Any


def build_owned_group_adapter(
    db: Any,
    *,
    allow_read_only: bool = False,
):
    """Build the concrete adapter when the requested operation is safe.

    Mutating worker calls are disabled unless ``OWNED_GROUP_EXECUTION_ENABLED``
    is explicitly true.  Reconciliation is read-only and may be requested while
    the write switch is off, so callers can opt into that path with
    ``allow_read_only=True``.  The adapter itself still performs the immediate
    Redis/global-stop and resource checks before every write; this factory only
    controls whether an adapter is available for the caller.
    """

    from app.core.config import settings

    execution_enabled = bool(getattr(settings, "OWNED_GROUP_EXECUTION_ENABLED", False))
    if not execution_enabled and not allow_read_only:
        return None

    # Keep these imports lazy.  API and Celery module import must remain safe in
    # environments where Telethon or the account pool has not been initialized.
    from app.core.account.pool import get_account_pool
    from app.core.account.risk_guard import AccountRiskGuard
    from app.core.account.telegram_execution import TelegramExecutionService
    from app.modules.owned_group.telegram_adapter import TelethonOwnedGroupTelegramAdapter

    return TelethonOwnedGroupTelegramAdapter(
        db=db,
        pool=get_account_pool(),
        execution_service=TelegramExecutionService(AccountRiskGuard(db)),
        # Redis-backed safety state is fail-closed for both read and write
        # adapter paths.  This is intentionally not configurable per request.
        require_redis_safety=True,
    )


__all__ = ["build_owned_group_adapter"]
