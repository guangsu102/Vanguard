"""Small live snapshots for the simplified worker; no full inbox history scans."""
from __future__ import annotations

import json
import time
from typing import Any
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime

from sqlalchemy import func, select

from app.core.group.models import GroupAccountMembership as Member
from app.core.operating_time import operating_day_start
from app.core.worker_status import TelegramEventInbox as Inbox
from app.modules.acquisition.models import AdDeliveryLog


async def aggregate(db: AsyncSession, cache: Any) -> dict:
    from app.core.metrics import GAUGE_LABELS, SNAPSHOT
    labels = {"vanguard_send_result_today": ("outcome",),
              "vanguard_send_result_memberships": ("state",),
              "vanguard_send_result_inbox_live": ("state",),
              "vanguard_send_result_inbox_oldest_age_seconds": (),
              "vanguard_send_result_sending_oldest_age_seconds": (),
              "vanguard_send_result_survival_enabled": ()}
    GAUGE_LABELS.update(labels)
    now = datetime.utcnow()
    gauges = [{"name": "vanguard_send_result_survival_enabled", "labels": [], "value": 0}]
    rows = (await db.execute(select(AdDeliveryLog.status, func.count()).where(
        AdDeliveryLog.created_at >= operating_day_start(now)).group_by(AdDeliveryLog.status))).all()
    gauges.extend({"name": "vanguard_send_result_today", "labels": [state], "value": count} for state, count in rows)
    for label, predicate in (
        ("cooldown", (Member.status == "joined") & (Member.next_ad_allowed_at > now)),
        ("exit_pending", Member.leave_requested_at.is_not(None) & Member.leave_retry_at.is_not(None)),
        ("exit_failed", Member.leave_requested_at.is_not(None) & (Member.leave_attempts >= 3)),
    ):
        value = await db.scalar(select(func.count(Member.id)).where(predicate)) or 0
        gauges.append({"name": "vanguard_send_result_memberships", "labels": [label], "value": value})
    live = (await db.execute(select(Inbox.state, func.count()).where(Inbox.state.in_(
        ["pending", "processing", "executing", "reconciliation_required"])).group_by(Inbox.state))).all()
    gauges.extend({"name": "vanguard_send_result_inbox_live", "labels": [state], "value": count} for state, count in live)
    from app.modules.acquisition.send_result_policy import POLICY
    for name, query in (
        ("vanguard_send_result_inbox_oldest_age_seconds", select(func.min(Inbox.received_at)).where(
            Inbox.state.in_(["pending", "processing", "executing", "reconciliation_required"]))),
        ("vanguard_send_result_sending_oldest_age_seconds", select(func.min(AdDeliveryLog.created_at)).where(
            AdDeliveryLog.status == "sending", AdDeliveryLog.qualification_context_json.contains(POLICY))),
    ):
        oldest = await db.scalar(query)
        gauges.append({"name": name, "labels": [], "value": max(0, (now - oldest).total_seconds()) if oldest else 0})
    snapshot = {"at": time.time(), "gauges": gauges, "policy": "send_result_v1", "accounts": []}
    await cache.set(SNAPSHOT, json.dumps(snapshot))
    from app.modules.acquisition.send_result_retention import clean_completed
    await clean_completed(db)
    return snapshot


def install(app: Any) -> None:
    """Install on older API images without replacing their existing lifespan."""
    if any(getattr(route, "path", None) == "/metrics" for route in app.routes):
        return
    import asyncio
    import secrets
    from contextlib import asynccontextmanager, suppress
    from fastapi import Request, Response
    from app.core.config import settings
    original = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(application: Any):
        async with original(application):
            from app.core.metrics import aggregation_loop
            task = asyncio.create_task(aggregation_loop())
            try:
                yield
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
    app.router.lifespan_context = lifespan
    async def endpoint(request: Request) -> Response:
        if not settings.METRICS_SCRAPE_TOKEN:
            return Response(status_code=503)
        if not secrets.compare_digest(request.headers.get("Authorization", ""), "Bearer " + settings.METRICS_SCRAPE_TOKEN):
            return Response(status_code=401)
        from app.core.metrics import render
        from app.core.redis import get_redis
        return Response(await render(await get_redis()), media_type="text/plain; version=0.0.4")
    # Postponed local annotations cannot be resolved by FastAPI in module globals.
    endpoint.__annotations__["request"] = Request
    endpoint.__annotations__["return"] = Response
    app.add_api_route("/metrics", endpoint, methods=["GET"], include_in_schema=False)
