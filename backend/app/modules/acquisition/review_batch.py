"""Two serial reviews share one fenced account lease for at most 60 seconds."""

import time
from contextvars import ContextVar
from typing import Any

from app.modules.acquisition.survival_reads import SurvivalReadBatch

current_batch: ContextVar[Any] = ContextVar("qualification_connection_batch", default=None)


class ReviewBatch(SurvivalReadBatch):
    async def acquire(self, account_id: int, *, restricted: bool = False) -> Any:
        if self.wrapper is not None and (
            self.key != (account_id, "group_qualification") or self.records >= 2
            or time.monotonic() - self.started_at >= 60
            or getattr(self.wrapper, "operation_lease_lost", False)
            or not self.wrapper.client or not self.wrapper.client.is_connected()
        ):
            await self.close()
        if self.wrapper is None:
            self.wrapper = await self.pool.acquire_by_id(
                account_id, purpose="group_qualification", raise_on_lease_failure=True,
                allow_restricted=restricted,
            )
            self.key = (account_id, "group_qualification")
            self.started_at = time.monotonic()
        self.records += 1
        return self.wrapper
