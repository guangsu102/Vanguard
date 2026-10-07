"""Coalesce the growth wakeup before publishing, including while workers are down."""
from celery.beat import PersistentScheduler
from redis import Redis

from app.core.config import settings


class BoundedGrowthScheduler(PersistentScheduler):
    def apply_async(self, entry, producer=None, advance=True, **kwargs):
        if entry.task != "app.core.scheduler.growth_dispatch.dispatch_growth_task":
            return super().apply_async(entry, producer, advance, **kwargs)
        # There is only one periodic producer. Broker inspection bounds physical
        # messages even when consumers cannot discard expired Celery messages.
        try:
            with Redis.from_url(settings.CELERY_BROKER_URL, socket_timeout=3) as broker:
                pending = sum(broker.llen("growth_dispatch" + (f"\x06\x16{p}" if p else ""))
                              for p in (0, 3, 6, 9))
        except Exception:
            pending = 1  # Fail closed; durable business work remains in the DB.
        if pending:
            if advance:
                self.reserve(entry)
            return None
        return super().apply_async(entry, producer, advance, **kwargs)
