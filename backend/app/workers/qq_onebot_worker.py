"""Long-running NapCatQQ OneBot 11 event worker."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import signal
from datetime import datetime
from typing import Any

import aiohttp
import structlog
from sqlalchemy import select

from app.core.config import settings
from app.core.worker_status import (
    TelegramWorkerRole,
    TelegramWorkerStatus,
    TelegramWorkerStatusValue,
)
from app.integrations.qq import OneBotAPIError, OneBotClient
from app.modules.qq.models import QQBotConnection
from app.modules.qq.service import QQEventProcessor, ensure_qq_connection

logger = structlog.get_logger()


class QQOneBotReconnect(RuntimeError):
    pass


class QQOneBotWorker:
    def __init__(self, worker_id: str) -> None:
        self.worker_id = worker_id
        self.client = OneBotClient()
        self.connection_id: int | None = None
        self._stop_event = asyncio.Event()

    def request_stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> int:
        if not settings.QQ_ONEBOT_ENABLED:
            logger.info("qq_onebot_worker_disabled")
            await self.client.close()
            return 0
        if not self.client.configured or not self.client.websocket_url:
            logger.error("qq_onebot_worker_not_configured")
            await self.client.close()
            return 2

        await self._ensure_runtime_records()
        await self._set_status(TelegramWorkerStatusValue.STARTING)
        backoff = 2
        try:
            while not self._stop_event.is_set():
                try:
                    await self._connect_and_consume()
                    backoff = 2
                except asyncio.CancelledError:
                    raise
                except (QQOneBotReconnect, OneBotAPIError) as exc:
                    logger.warning("qq_onebot_connection_degraded", error=str(exc))
                    await self._set_status(
                        TelegramWorkerStatusValue.DEGRADED,
                        last_error=str(exc),
                    )
                except Exception as exc:
                    logger.exception("qq_onebot_connection_failed", error=str(exc))
                    await self._set_status(
                        TelegramWorkerStatusValue.ERROR,
                        last_error=str(exc),
                    )
                if self._stop_event.is_set():
                    break
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                backoff = min(60, backoff * 2)
        finally:
            await self._set_status(TelegramWorkerStatusValue.OFFLINE)
            await self.client.close()
        return 0

    async def _connect_and_consume(self) -> None:
        await self._refresh_login_and_groups()
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=None)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(
                self.client.websocket_url,
                headers=self.client.websocket_headers,
                heartbeat=30,
                receive_timeout=90,
            ) as ws:
                await self._set_status(TelegramWorkerStatusValue.ONLINE)
                logger.info(
                    "qq_onebot_websocket_connected",
                    account_id=self.client.account_id,
                )
                async for message in ws:
                    if self._stop_event.is_set():
                        await ws.close()
                        break
                    if message.type == aiohttp.WSMsgType.TEXT:
                        try:
                            payload = json.loads(message.data)
                        except json.JSONDecodeError:
                            logger.warning("qq_onebot_invalid_json")
                            continue
                        if isinstance(payload, dict):
                            await self._handle_payload(payload)
                    elif message.type in {
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    }:
                        raise QQOneBotReconnect("NapCat OneBot WebSocket closed")

    async def _handle_payload(self, payload: dict[str, Any]) -> None:
        self_id = str(payload.get("self_id") or "")
        if self_id and self_id != self.client.account_id:
            logger.warning(
                "qq_onebot_unexpected_account",
                expected=self.client.account_id,
                received=self_id,
            )
            return
        if payload.get("post_type") == "meta_event":
            if payload.get("meta_event_type") == "heartbeat":
                status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
                if status.get("online") is False or status.get("good") is False:
                    await self._set_status(
                        TelegramWorkerStatusValue.DEGRADED,
                        last_error="NapCat reported an unhealthy QQ session",
                    )
                else:
                    await self._record_heartbeat()
            return
        await self._process_event(payload)

    async def _process_event(self, payload: dict[str, Any]) -> None:
        from app.core import database as db_module
        from app.core import redis as redis_module

        async with db_module.get_db_session() as db:
            connection = await db.get(QQBotConnection, self.connection_id)
            if connection is None:
                return
            processor = QQEventProcessor(db, redis_module.redis_client)
            result = await processor.handle_onebot_event(connection, payload)
            if result is not None:
                channel, event_type, serialized = result
                await processor.publish(channel, event_type, serialized)

    async def _ensure_runtime_records(self) -> None:
        from app.core import database as db_module

        async with db_module.get_db_session() as db:
            connection = await ensure_qq_connection(db, self.client.account_id)
            self.connection_id = connection.id

    async def _refresh_login_and_groups(self) -> None:
        from app.core import database as db_module
        from app.core import redis as redis_module

        login = await self.client.get_login_info()
        logged_account = str(login.get("user_id") or "")
        if logged_account != self.client.account_id:
            raise OneBotAPIError(
                f"NapCat logged in as QQ {logged_account or 'unknown'}, expected {self.client.account_id}"
            )
        group_rows = await self.client.get_group_list()
        now = datetime.utcnow()
        async with db_module.get_db_session() as db:
            connection = await ensure_qq_connection(
                db,
                self.client.account_id,
                display_name=str(login.get("nickname") or "").strip() or None,
            )
            self.connection_id = connection.id
            connection.bot_openid = self.client.account_id
            connection.status = TelegramWorkerStatusValue.ONLINE.value
            connection.last_connected_at = now
            connection.last_heartbeat_at = now
            connection.last_error = None
            processor = QQEventProcessor(db, redis_module.redis_client)
            groups = await processor.sync_groups(connection, group_rows)
            await processor.publish(
                "qq:groups",
                "qq:groups-synced",
                {"account_id": self.client.account_id, "total": len(groups)},
            )

    async def _record_heartbeat(self) -> None:
        from app.core import database as db_module

        now = datetime.utcnow()
        async with db_module.get_db_session() as db:
            if self.connection_id is not None:
                connection = await db.get(QQBotConnection, self.connection_id)
                if connection is not None:
                    connection.status = TelegramWorkerStatusValue.ONLINE.value
                    connection.last_heartbeat_at = now
                    connection.last_error = None
            result = await db.execute(
                select(TelegramWorkerStatus).where(
                    TelegramWorkerStatus.worker_id == self.worker_id
                )
            )
            worker = result.scalar_one_or_none()
            if worker is not None:
                worker.status = TelegramWorkerStatusValue.ONLINE.value
                worker.last_heartbeat_at = now
                worker.last_error = None

    async def _set_status(
        self,
        status: TelegramWorkerStatusValue,
        *,
        last_error: str | None = None,
    ) -> None:
        from app.core import database as db_module

        now = datetime.utcnow()
        metadata = json.dumps(
            {
                "provider": "napcat_onebot11",
                "account_id": self.client.account_id,
                "connection_id": self.connection_id,
                "http_url": self.client.http_url,
                "websocket_url": self.client.websocket_url,
            },
            separators=(",", ":"),
        )
        async with db_module.get_db_session() as db:
            result = await db.execute(
                select(TelegramWorkerStatus).where(
                    TelegramWorkerStatus.worker_id == self.worker_id
                )
            )
            worker = result.scalar_one_or_none()
            if worker is None:
                worker = TelegramWorkerStatus(
                    worker_id=self.worker_id,
                    role=TelegramWorkerRole.QQ_ONEBOT.value,
                    status=status.value,
                )
                db.add(worker)
            worker.role = TelegramWorkerRole.QQ_ONEBOT.value
            worker.status = status.value
            worker.last_heartbeat_at = now
            worker.last_error = last_error[:2000] if last_error else None
            worker.metadata_json = metadata
            if self.connection_id is not None:
                connection = await db.get(QQBotConnection, self.connection_id)
                if connection is not None:
                    connection.status = status.value
                    connection.last_heartbeat_at = now
                    connection.last_error = worker.last_error


async def async_main(worker_id: str) -> int:
    from app.core import database as db_module
    from app.core import redis as redis_module

    await db_module.init_db(create_tables=False)
    await redis_module.init_redis()
    worker = QQOneBotWorker(worker_id)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, worker.request_stop)
    try:
        return await worker.run()
    finally:
        await redis_module.close_redis()
        await db_module.close_db()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the NapCatQQ OneBot 11 worker")
    parser.add_argument("--worker-id", default="qq_onebot_worker:local")
    args = parser.parse_args()
    return asyncio.run(async_main(args.worker_id))


if __name__ == "__main__":
    raise SystemExit(main())
