"""Known read waits postpone work without recording a technical failure."""

from datetime import datetime, timedelta
from typing import Any

from app.core.account.rpc_governor import RpcDeferred, check_read_ready


async def read_wait(
    db: Any,
    account_id: int,
    now: datetime,
    *,
    purpose: str,
    requires_bootstrap: bool = False,
    minimum_reads: int = 1,
    work_group_id: int | None = None, work_scope: str = "",
    risk_pause_until: datetime | None = None,
) -> tuple[str, datetime] | None:
    wait = None
    try:
        await check_read_ready(
            db, account_id, now, purpose=purpose, requires_bootstrap=requires_bootstrap,
            **({"minimum_reads": minimum_reads} if minimum_reads > 1 else {}),
            **({"work_group_id": work_group_id, "work_scope": work_scope} if work_group_id is not None else {}),
        )
    except RpcDeferred as exc:
        wait = (exc.reason, now + timedelta(seconds=exc.retry_after_seconds + 1))
    if risk_pause_until and risk_pause_until > now:
        if wait is None or risk_pause_until > wait[1]:
            wait = ("account_risk_paused", risk_pause_until + timedelta(seconds=1))
    return wait
