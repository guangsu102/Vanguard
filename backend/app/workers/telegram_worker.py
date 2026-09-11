"""Unified Telegram worker entrypoint.

This module establishes the runtime contract for the converged architecture:
growth user workers and guardian bot workers read backend configuration and
report heartbeat state back to the backend database.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import socket
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, Literal, Optional

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from telethon import events as telethon_events

from app.core.account.models import (
    AccountOperationConfig,
    AccountStatus,
    AccountType,
    GuardianBotHealthStatus,
    GuardianBotProfile,
    TelegramAccount,
)
from app.core.account.pool import AccountPool, _resolve_account_api_credentials
from app.core.account.proxy_policy_events import (
    start_account_proxy_policy_listener,
    stop_account_proxy_policy_listener,
)
from app.core.account.risk_guard import AccountRiskGuard
from app.core.account.system_identity import bot_risk_identity
from app.core.account.telegram_execution import (
    TelegramExecutionError,
    TelegramExecutionService,
)
from app.core.config import settings
from app.core.database import close_db, get_db_session, init_db
from app.core.keyword.models import Keyword, KeywordStatus
from app.core.logging import setup_logging
from app.core.redis import close_redis, init_redis
from app.core.telegram_chat_lock import acquire_telegram_chat_transaction_lock
from app.core.worker_status import (
    TelegramWorkerRole,
    TelegramWorkerStatus,
    TelegramWorkerStatusValue,
)
from app.integrations.telegram.client import TelegramClient, TelegramConfig
from app.modules.acquisition.handler import AcquisitionEventHandler, MemberJoinEvent, MessageEvent
from app.modules.acquisition.models import (
    AdCampaign,
    GroupSearchKeyword,
    KeywordTrigger,
    MessageTemplate,
    SearchKeywordStatus,
)
from app.modules.guardian.main import create_guardian_bot
from app.modules.guardian.models import (
    ManagedGroupBinding,
    ManagedGroupBindingStatus,
    ManagedGroupBotRole,
    ModerationRule,
    ModerationSensitiveKeyword,
)
from app.modules.guardian.sync import (
    ManagedGroupSyncConflict,
    guardian_role_and_status_from_member,
    sync_managed_group_binding,
)
from app.modules.owned_group.governance_worker import (
    add_owned_governance_audit,
    evaluate_guardian_member,
    owned_governance_audit_state,
    owned_group_governance_gate_reason,
    resolve_governance_worker_target,
)
from app.modules.owned_group.member_observation import (
    project_owned_group_member_observations,
)
from app.modules.owned_group.messaging_event_router import (
    OwnedGroupMessagingEventRouter,
)
from app.modules.owned_group.models import OwnedGroupAsset
from app.modules.owned_group.models_extra import OwnedBotProfile
from app.modules.owned_group.security import safe_exception_message
from app.modules.private_chat.service import (
    IncomingPrivateMessage,
    claim_pending_outbound_message,
    finalize_outbound_private_message,
    is_conversation_auto_reply_enabled,
    persist_incoming_private_message,
    publish_private_chat_event,
    serialize_conversation,
    serialize_private_message,
)

logger = structlog.get_logger()

ENTITY_NAME_CACHE_TTL_SECONDS = 30 * 60
ENTITY_NAME_CACHE_MAX_SIZE = 5000
GROWTH_EVENT_CONCURRENCY_CAP = 8
GROWTH_EVENT_DB_CONNECTION_RESERVE = 2


class TelegramWorker:
    def __init__(self, role: TelegramWorkerRole, worker_id: Optional[str] = None, heartbeat_interval: int = 30):
        self.role = role
        self.worker_id = worker_id or f"{role.value}:{socket.gethostname()}"
        self.heartbeat_interval = heartbeat_interval
        self._running = False
        self._account_pool: Optional[AccountPool] = AccountPool() if role == TelegramWorkerRole.GROWTH_USER else None
        self._guardian_update_offsets: dict[int, int] = {}
        self._growth_listener_sessions: dict[int, str] = {}
        self._entity_name_cache: dict[int, tuple[float, str]] = {}
        self._growth_handler_init_options: dict[str, bool] = {}
        self._last_daily_health_log_date: str | None = None
        self._growth_event_concurrency = min(
            GROWTH_EVENT_CONCURRENCY_CAP,
            max(1, settings.DATABASE_POOL_SIZE - GROWTH_EVENT_DB_CONNECTION_RESERVE),
        )
        self._growth_event_semaphore = asyncio.BoundedSemaphore(
            self._growth_event_concurrency
        )
        self._private_outbox_task: asyncio.Task[None] | None = None

    async def run(self) -> None:
        await init_db(create_tables=not settings.is_production)
        await init_redis()
        await start_account_proxy_policy_listener()
        self._running = True
        try:
            await self._heartbeat(TelegramWorkerStatusValue.STARTING.value, {"phase": "startup"})
            while self._running:
                await self.run_once()
                if (
                    self.role == TelegramWorkerRole.GROWTH_USER
                    and self._private_outbox_task is None
                ):
                    self._private_outbox_task = asyncio.create_task(
                        self._private_outbox_loop()
                    )
                await asyncio.sleep(self.heartbeat_interval)
        except asyncio.CancelledError:
            await self._heartbeat(TelegramWorkerStatusValue.OFFLINE.value, {"phase": "cancelled"})
            raise
        except Exception as exc:
            logger.error(
                "telegram_worker_failed",
                worker_id=self.worker_id,
                role=self.role.value,
                error_type=type(exc).__name__,
                error=safe_exception_message(exc, max_length=500),
            )
            await self._heartbeat(TelegramWorkerStatusValue.ERROR.value, {"phase": "error"}, last_error=str(exc))
            raise
        finally:
            if self._private_outbox_task is not None:
                self._private_outbox_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._private_outbox_task
                self._private_outbox_task = None
            if self._account_pool is not None:
                await self._account_pool.close_all()
            await stop_account_proxy_policy_listener()
            await close_redis()
            await close_db()

    async def stop(self) -> None:
        self._running = False
        await self._heartbeat(TelegramWorkerStatusValue.OFFLINE.value, {"phase": "shutdown"})

    async def run_once(self) -> dict[str, Any]:
        snapshot = await self._load_configuration_snapshot()
        self._update_growth_handler_init_options(snapshot)
        cycle_metadata = await self._run_role_cycle(snapshot)
        snapshot.update(cycle_metadata)
        status = self._status_for_snapshot(snapshot)
        await self._heartbeat(status, snapshot)
        self._log_daily_health(status, snapshot)
        return {"status": status, "metadata": snapshot}

    def _update_growth_handler_init_options(self, snapshot: dict[str, Any]) -> None:
        if self.role != TelegramWorkerRole.GROWTH_USER:
            return
        self._growth_handler_init_options = {
            "load_keywords": snapshot.get("active_keywords", 0) > 0,
            "load_triggers": snapshot.get("enabled_keyword_triggers", 0) > 0,
            "load_templates": snapshot.get("enabled_message_templates", 0) > 0,
        }

    def _log_daily_health(self, status: str, snapshot: dict[str, Any]) -> None:
        current_date = datetime.now(timezone.utc).date().isoformat()
        if self._last_daily_health_log_date == current_date:
            return
        self._last_daily_health_log_date = current_date
        logger.info(
            "telegram_worker_daily_health",
            worker_id=self.worker_id,
            role=self.role.value,
            status=status,
            enabled_accounts=snapshot.get("enabled_accounts"),
            enabled_bots=snapshot.get("enabled_bots"),
        )

    async def _heartbeat(self, status: str, metadata: dict[str, Any], last_error: Optional[str] = None) -> None:
        async with get_db_session() as db:
            result = await db.execute(select(TelegramWorkerStatus).where(TelegramWorkerStatus.worker_id == self.worker_id))
            row = result.scalar_one_or_none()
            now = datetime.utcnow()
            metadata_json = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
            if row is None:
                row = TelegramWorkerStatus(
                    worker_id=self.worker_id,
                    role=self.role.value,
                    status=status,
                    last_heartbeat_at=now,
                    last_error=last_error,
                    metadata_json=metadata_json,
                )
                db.add(row)
            else:
                row.role = self.role.value
                row.status = status
                row.last_heartbeat_at = now
                row.last_error = last_error
                row.metadata_json = metadata_json

    async def _load_configuration_snapshot(self) -> dict[str, Any]:
        async with get_db_session() as db:
            if self.role == TelegramWorkerRole.GROWTH_USER:
                accounts = (
                    await db.execute(
                        select(TelegramAccount)
                        .options(selectinload(TelegramAccount.static_proxy))
                        .join(AccountOperationConfig, AccountOperationConfig.account_id == TelegramAccount.id)
                        .where(TelegramAccount.account_type == AccountType.PROMOTER)
                        .where(TelegramAccount.is_active == True)
                        .where(TelegramAccount.status.notin_([AccountStatus.ERROR, AccountStatus.BANNED]))
                        .where(AccountOperationConfig.enabled == True)
                    )
                ).scalars().all()
                auto_join_enabled = (
                    await db.execute(
                        select(func.count(AccountOperationConfig.id)).where(
                            AccountOperationConfig.enabled == True,
                            AccountOperationConfig.auto_join_enabled == True,
                        )
                    )
                ).scalar() or 0
                auto_ads_enabled = (
                    await db.execute(
                        select(func.count(AccountOperationConfig.id)).where(
                            AccountOperationConfig.enabled == True,
                            AccountOperationConfig.auto_ads_enabled == True,
                        )
                    )
                ).scalar() or 0
                approved_search_keywords = (
                    await db.execute(
                        select(func.count(GroupSearchKeyword.id)).where(
                            GroupSearchKeyword.enabled == True,
                            GroupSearchKeyword.status == SearchKeywordStatus.APPROVED,
                        )
                    )
                ).scalar() or 0
                enabled_triggers = (
                    await db.execute(select(func.count(KeywordTrigger.id)).where(KeywordTrigger.enabled == True))
                ).scalar() or 0
                active_keywords = (
                    await db.execute(
                        select(func.count(Keyword.id)).where(
                            Keyword.status.in_(
                                [KeywordStatus.APPROVED, KeywordStatus.EXECUTING]
                            )
                        )
                    )
                ).scalar() or 0
                enabled_templates = (
                    await db.execute(select(func.count(MessageTemplate.id)).where(MessageTemplate.enabled == True))
                ).scalar() or 0
                enabled_ad_campaigns = (
                    await db.execute(select(func.count(AdCampaign.id)).where(AdCampaign.enabled == True))
                ).scalar() or 0
                return {
                    "role": self.role.value,
                    "enabled_accounts": len(accounts),
                    "auto_join_enabled_accounts": auto_join_enabled,
                    "auto_ads_enabled_accounts": auto_ads_enabled,
                    "approved_search_keywords": approved_search_keywords,
                    "active_keywords": active_keywords,
                    "enabled_keyword_triggers": enabled_triggers,
                    "enabled_message_templates": enabled_templates,
                    "enabled_ad_campaigns": enabled_ad_campaigns,
                    "config_source": "backend_database",
                    "execution_surface": "acquisition",
                }

            bots = (
                await db.execute(
                    select(GuardianBotProfile)
                    .join(TelegramAccount, GuardianBotProfile.account_id == TelegramAccount.id)
                    .where(TelegramAccount.account_type == AccountType.GUARDIAN_BOT)
                    .where(TelegramAccount.is_active == True)
                    .where(GuardianBotProfile.enabled == True)
                )
            ).scalars().all()
            active_bindings = (
                await db.execute(
                    select(func.count(ManagedGroupBinding.id)).where(
                        ManagedGroupBinding.binding_status == ManagedGroupBindingStatus.ACTIVE,
                    )
                )
            ).scalar() or 0
            enabled_rules = (
                await db.execute(select(func.count(ModerationRule.id)).where(ModerationRule.enabled == True))
            ).scalar() or 0
            enabled_sensitive_keywords = (
                await db.execute(select(func.count(ModerationSensitiveKeyword.id)).where(ModerationSensitiveKeyword.enabled == True))
            ).scalar() or 0
            return {
                "role": self.role.value,
                "enabled_bots": len(bots),
                "active_group_bindings": active_bindings,
                "enabled_moderation_rules": enabled_rules,
                "enabled_sensitive_keywords": enabled_sensitive_keywords,
                "config_source": "backend_database",
                "execution_surface": "guardian",
            }

    async def _run_role_cycle(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        if self.role == TelegramWorkerRole.GROWTH_USER:
            return await self._run_growth_user_cycle()
        return await self._run_guardian_bot_cycle(snapshot)

    async def _run_growth_user_cycle(self) -> dict[str, Any]:
        if self._account_pool is None:
            return {"runtime": {"account_pool_size": 0, "account_pool_synced": 0}}

        async with get_db_session() as db:
            accounts = (
                await db.execute(
                    select(TelegramAccount)
                        .options(selectinload(TelegramAccount.static_proxy))
                        .join(AccountOperationConfig, AccountOperationConfig.account_id == TelegramAccount.id)
                        .where(TelegramAccount.account_type == AccountType.PROMOTER)
                        .where(TelegramAccount.is_active == True)
                        .where(TelegramAccount.status.notin_([AccountStatus.ERROR, AccountStatus.BANNED]))
                        .where(AccountOperationConfig.enabled == True)
                    )
                ).scalars().all()

        runtime_accounts = [account for account in accounts if self._is_runtime_capable_account(account)]
        synced = await self._account_pool.sync_from_db(runtime_accounts)
        listener_state = await self._ensure_growth_listeners(runtime_accounts)
        pool_stats = await self._account_pool.health_check()
        return {
            "runtime": {
                "account_pool_size": self._account_pool.size,
                "account_pool_synced": synced,
                "runtime_capable_accounts": len(runtime_accounts),
                "growth_event_concurrency_limit": self._growth_event_concurrency,
                "runtime_skipped_accounts": len(accounts) - len(runtime_accounts),
                **listener_state,
                "account_pool": pool_stats,
                "listener": "telethon_user_listener",
            }
        }

    def _is_runtime_capable_account(self, account: TelegramAccount) -> bool:
        api_id, api_hash = _resolve_account_api_credentials(account)
        return bool(account.phone and api_id and api_hash)

    async def _ensure_growth_listeners(self, accounts: list[TelegramAccount]) -> dict[str, Any]:
        if self._account_pool is None:
            return {"active_listeners": 0, "listeners_started": 0, "listeners_stopped": 0, "listener_errors": []}

        active_account_ids = {account.id for account in accounts}
        stopped = 0
        errors: list[dict[str, Any]] = []

        for account_id, session_name in list(self._growth_listener_sessions.items()):
            wrapper = await self._account_pool.get_account_by_id(account_id)
            connected = bool(wrapper and wrapper.client and wrapper.client.is_connected())
            if account_id not in active_account_ids or not connected:
                await self._account_pool.set_offline(session_name)
                self._growth_listener_sessions.pop(account_id, None)
                stopped += 1

        started = 0
        for account in accounts:
            if account.id in self._growth_listener_sessions:
                try:
                    existing = await self._account_pool.get_account_by_id(account.id)
                    previous_client = existing.client if existing is not None else None
                    wrapper = await self._account_pool.connect_by_id(
                        account.id,
                        purpose="growth_listener_refresh",
                        require_session=True,
                        keep_connected=True,
                    )
                    if wrapper is not None and wrapper.client is not None and wrapper.client is not previous_client:
                        self._attach_growth_event_handlers(wrapper)
                except Exception as exc:
                    await self._account_pool.set_offline(account.session_name)
                    self._growth_listener_sessions.pop(account.id, None)
                    errors.append({"account_id": account.id, "error": str(exc)})
                    stopped += 1
                    logger.warning("growth_listener_refresh_failed", account_id=account.id, error=str(exc))
                continue
            try:
                wrapper = await self._account_pool.connect_by_id(
                    account.id,
                    purpose="growth_listener",
                    require_session=True,
                    keep_connected=True,
                )
                if wrapper is None or wrapper.client is None:
                    errors.append({"account_id": account.id, "error": "account_not_connectable"})
                    continue
                self._attach_growth_event_handlers(wrapper)
                self._growth_listener_sessions[account.id] = wrapper.session_name
                started += 1
            except Exception as exc:
                errors.append({"account_id": account.id, "error": str(exc)})
                logger.warning("growth_listener_start_failed", account_id=account.id, error=str(exc))

        return {
            "active_listeners": len(self._growth_listener_sessions),
            "listeners_started": started,
            "listeners_stopped": stopped,
            "listener_errors": errors[:5],
        }

    def _attach_growth_event_handlers(self, account: Any) -> None:
        client = account.client
        if client is None:
            return

        account_id = account.account_id

        async def handle_new_message(event: Any) -> None:
            await self._run_growth_event(
                self._handle_growth_new_message, account_id, event
            )

        async def handle_chat_action(event: Any) -> None:
            await self._run_growth_event(
                self._handle_growth_chat_action, account_id, event
            )

        client.add_event_handler(handle_new_message, telethon_events.NewMessage(incoming=True))
        client.add_event_handler(handle_chat_action, telethon_events.ChatAction())
        logger.info("growth_event_handlers_attached", account_id=account_id, session_name=account.session_name)

    async def _run_growth_event(
        self,
        handler: Callable[[int, Any], Awaitable[None]],
        account_id: int,
        event: Any,
    ) -> None:
        async with self._growth_event_semaphore:
            await handler(account_id, event)

    def _get_cached_entity_name(self, user_id: int) -> Optional[str]:
        cached = self._entity_name_cache.get(int(user_id))
        if cached is None:
            return None
        expires_at, name = cached
        if expires_at <= time.monotonic():
            self._entity_name_cache.pop(int(user_id), None)
            return None
        return name

    def _cache_entity_name(self, user_id: int, name: str) -> None:
        if not name:
            return
        now = time.monotonic()
        if len(self._entity_name_cache) >= ENTITY_NAME_CACHE_MAX_SIZE:
            expired = [
                key
                for key, (expires_at, _cached_name) in self._entity_name_cache.items()
                if expires_at <= now
            ]
            for key in expired:
                self._entity_name_cache.pop(key, None)
            while len(self._entity_name_cache) >= ENTITY_NAME_CACHE_MAX_SIZE:
                oldest_key = next(iter(self._entity_name_cache))
                self._entity_name_cache.pop(oldest_key, None)
        self._entity_name_cache[int(user_id)] = (now + ENTITY_NAME_CACHE_TTL_SECONDS, name)

    @staticmethod
    def _display_name_from_entity(entity: Any, fallback: str) -> str:
        return (
            getattr(entity, "username", None)
            or getattr(entity, "first_name", None)
            or getattr(entity, "title", None)
            or fallback
        )

    async def _resolve_sender_name_from_event(self, event: Any, sender_id: int) -> str:
        cached = self._get_cached_entity_name(sender_id)
        if cached:
            return cached

        fallback = str(sender_id)
        sender = getattr(event, "sender", None)
        if sender is None:
            sender = getattr(getattr(event, "message", None), "sender", None)
        if sender is None:
            sender = await event.get_sender()

        name = self._display_name_from_entity(sender, fallback)
        self._cache_entity_name(sender_id, name)
        return name

    async def _resolve_entity_name_from_client(self, client: Any, user_id: int) -> str:
        cached = self._get_cached_entity_name(user_id)
        if cached:
            return cached

        fallback = str(user_id)
        entity = await client.get_entity(user_id)
        name = self._display_name_from_entity(entity, fallback)
        self._cache_entity_name(user_id, name)
        return name

    @staticmethod
    def _private_sender_metadata(event: Any, sender_id: int) -> tuple[Optional[str], str]:
        message = getattr(event, "message", None)
        sender = getattr(event, "sender", None) or getattr(message, "sender", None)
        username = getattr(sender, "username", None)
        display_name = " ".join(
            part
            for part in (
                getattr(sender, "first_name", None),
                getattr(sender, "last_name", None),
            )
            if part
        )
        return username, display_name or username or str(sender_id)

    @staticmethod
    def _private_message_media(message: Any) -> tuple[str, Optional[dict[str, Any]]]:
        if message is None or getattr(message, "media", None) is None:
            return "text", None

        message_type = "media"
        media_object = getattr(message, "media", None)
        for attribute, candidate in (
            ("photo", "photo"),
            ("sticker", "sticker"),
            ("voice", "voice"),
            ("video", "video"),
            ("audio", "audio"),
            ("document", "document"),
            ("contact", "contact"),
            ("geo", "geo"),
        ):
            value = getattr(message, attribute, None)
            if value is not None:
                message_type = candidate
                media_object = value
                break

        metadata: dict[str, Any] = {"kind": message_type}
        media_id = getattr(media_object, "id", None)
        if media_id is not None:
            metadata["id"] = str(media_id)
        file_info = getattr(message, "file", None)
        for key, attribute in (
            ("name", "name"),
            ("mime_type", "mime_type"),
            ("size", "size"),
        ):
            value = getattr(file_info, attribute, None)
            if value is not None:
                metadata[key] = value
        return message_type, metadata

    async def _handle_growth_new_message(self, account_id: int, event: Any) -> None:
        text = getattr(event, "raw_text", None) or getattr(event, "text", None) or ""
        sender_id = getattr(event, "sender_id", None)
        chat_id = getattr(event, "chat_id", None)
        telegram_message = getattr(event, "message", None)
        message_id = getattr(event, "id", None) or getattr(telegram_message, "id", None)
        if sender_id is None or chat_id is None:
            return

        sender_name = self._get_cached_entity_name(int(sender_id)) or str(sender_id)
        is_private = bool(getattr(event, "is_private", False))

        occurred_at = getattr(telegram_message, "date", None)
        if not isinstance(occurred_at, datetime):
            occurred_at = datetime.utcnow()
        elif occurred_at.tzinfo is not None:
            occurred_at = occurred_at.astimezone(timezone.utc).replace(tzinfo=None)

        if is_private:
            if message_id is None:
                logger.warning(
                    "private_message_missing_telegram_id",
                    account_id=account_id,
                    sender_id=sender_id,
                )
                return
            peer_username, peer_display_name = self._private_sender_metadata(
                event, int(sender_id)
            )
            message_type, media = self._private_message_media(telegram_message)
            reply_to_message_id = getattr(telegram_message, "reply_to_msg_id", None)
            should_auto_reply = False
            created = False
            try:
                async with get_db_session() as db:
                    conversation, private_message, created = (
                        await persist_incoming_private_message(
                            db,
                            IncomingPrivateMessage(
                                account_id=account_id,
                                peer_telegram_id=int(sender_id),
                                telegram_message_id=int(message_id),
                                content=text or None,
                                occurred_at=occurred_at,
                                peer_username=peer_username,
                                peer_display_name=peer_display_name,
                                message_type=message_type,
                                media=media,
                                reply_to_telegram_message_id=(
                                    int(reply_to_message_id)
                                    if reply_to_message_id is not None
                                    else None
                                ),
                            ),
                        )
                    )
                    if created:
                        should_auto_reply = bool(text.strip()) and (
                            await is_conversation_auto_reply_enabled(db, conversation)
                        )
                        conversation_data = serialize_conversation(conversation)
                        message_data = serialize_private_message(private_message)

                if not created:
                    return
                await publish_private_chat_event(
                    "telegram:private-conversation", conversation_data
                )
                await publish_private_chat_event(
                    "telegram:private-message", message_data
                )
                if not should_auto_reply:
                    return
            except Exception as exc:
                logger.warning(
                    "private_message_persist_failed",
                    account_id=account_id,
                    sender_id=sender_id,
                    message_id=message_id,
                    error=str(exc),
                )
                return
        else:
            telegram_sender = getattr(event, "sender", None)
            reply_to_message_id = getattr(
                telegram_message,
                "reply_to_msg_id",
                None,
            )
            mentioned_usernames = tuple(
                match.casefold()
                for match in re.findall(
                    r"@([A-Za-z0-9_]{3,64})",
                    str(text or ""),
                )
            )

            async def resolve_mentioned_user_id(username: str) -> Optional[int]:
                client = getattr(event, "client", None)
                if client is None:
                    return None
                entity = await client.get_entity(f"@{username}")
                entity_id = getattr(entity, "id", None)
                return int(entity_id) if entity_id is not None else None

            try:
                async with get_db_session() as db:
                    route = await OwnedGroupMessagingEventRouter(
                        db
                    ).route_incoming(
                        telegram_chat_id=int(chat_id),
                        source_message_id=int(message_id or 0),
                        sender_id=int(sender_id),
                        sender_name=sender_name,
                        text=str(text or ""),
                        occurred_at=occurred_at,
                        listener_account_id=int(account_id),
                        reply_to_message_id=(
                            int(reply_to_message_id)
                            if reply_to_message_id is not None
                            else None
                        ),
                        mentioned_usernames=mentioned_usernames,
                        mention_resolver=resolve_mentioned_user_id,
                        sender_is_bot=bool(
                            getattr(telegram_sender, "bot", False)
                        ),
                        outgoing=bool(getattr(event, "out", False)),
                    )
                if route.owned_group_event:
                    logger.debug(
                        "owned_group_message_event_consumed",
                        account_id=account_id,
                        chat_id=chat_id,
                        message_id=message_id,
                        asset_id=route.asset_id,
                        execution_id=route.execution_id,
                        ignored_reason=route.ignored_reason,
                    )
                    return
            except Exception as exc:
                # A DB/router outage must fail closed. Falling through here can
                # make legacy keyword/semantic handlers duplicate owned sends.
                logger.warning(
                    "owned_group_message_route_failed_closed",
                    account_id=account_id,
                    chat_id=chat_id,
                    message_id=message_id,
                    error=safe_exception_message(exc, max_length=500),
                )
                return
            if not text:
                return

        async def resolve_sender_name() -> str:
            return await self._resolve_sender_name_from_event(event, int(sender_id))

        message_event = MessageEvent(
            message_id=int(message_id or 0),
            chat_id=int(chat_id),
            sender_id=int(sender_id),
            sender_name=sender_name,
            content=text,
            is_group=not is_private,
            timestamp=occurred_at,
            account_id=account_id,
            sender_name_resolver=resolve_sender_name,
        )

        try:
            async with get_db_session() as db:
                handler = AcquisitionEventHandler(db=db, account_pool=self._account_pool)
                await handler.initialize(**self._growth_handler_init_options)
                if text.strip().startswith("/"):
                    await handler.on_command(message_event)
                else:
                    await handler.on_message(message_event)
        except Exception as exc:
            logger.warning(
                "growth_message_dispatch_failed",
                account_id=account_id,
                chat_id=chat_id,
                message_id=message_id,
                error=str(exc),
            )

    async def _private_outbox_loop(self) -> None:
        while self._running:
            try:
                processed = await self._process_private_outbox_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                processed = False
                logger.error(
                    "private_outbox_loop_failed",
                    worker_id=self.worker_id,
                    error_type=type(exc).__name__,
                    error=safe_exception_message(exc, max_length=500),
                )
            if not processed:
                await asyncio.sleep(0.5)

    async def _process_private_outbox_once(self) -> bool:
        if self._account_pool is None:
            return False

        async with get_db_session() as db:
            message = await claim_pending_outbound_message(db)
            if message is None:
                return False
            message_id = message.id
            account_id = message.account_id
            peer_telegram_id = message.peer_telegram_id
            content = message.content or ""
            sending_data = serialize_private_message(message)

        await publish_private_chat_event(
            "telegram:private-message-status", sending_data
        )

        final_status = "sent"
        telegram_message_id = None
        error_message = None
        try:
            account = await self._account_pool.connect_by_id(
                account_id,
                purpose="private_chat_operator_reply",
                require_session=True,
                keep_connected=True,
            )
            if account is None:
                raise TelegramExecutionError("conversation account is unavailable")
            async with get_db_session() as db:
                execution = TelegramExecutionService(AccountRiskGuard(db))
                result = await execution.send_private_message_result(
                    account,
                    peer_telegram_id,
                    content,
                    initiated_by_user=True,
                    source="private_chat_operator",
                )
                result_id = getattr(result, "id", None) or getattr(
                    result, "message_id", None
                )
                telegram_message_id = int(result_id) if result_id is not None else None
        except TelegramExecutionError as exc:
            final_status = "failed"
            error_message = str(exc)
        except Exception as exc:
            final_status = "unknown"
            error_message = str(exc)
            logger.warning(
                "private_outbox_send_uncertain",
                message_id=message_id,
                account_id=account_id,
                error=str(exc),
            )

        async with get_db_session() as db:
            conversation, finalized = await finalize_outbound_private_message(
                db,
                message_id,
                status=final_status,
                telegram_message_id=telegram_message_id,
                error_message=error_message,
            )
            final_data = serialize_private_message(finalized)
            conversation_data = serialize_conversation(conversation)

        await publish_private_chat_event(
            "telegram:private-message-status", final_data
        )
        await publish_private_chat_event(
            "telegram:private-conversation", conversation_data
        )
        logger.info(
            "private_outbox_processed",
            message_id=message_id,
            account_id=account_id,
            status=final_status,
        )
        return True

    async def _handle_growth_chat_action(self, account_id: int, event: Any) -> None:
        if not self._event_flag(event, "user_joined") and not self._event_flag(event, "user_added"):
            return

        chat_id = getattr(event, "chat_id", None)
        if chat_id is None:
            return

        user_ids = list(getattr(event, "user_ids", None) or [])
        if not user_ids:
            user_id = getattr(event, "user_id", None)
            if user_id is not None:
                user_ids = [user_id]

        if not user_ids:
            return

        me_id = None
        try:
            me = await event.client.get_me()
            me_id = getattr(me, "id", None)
        except Exception:
            pass

        for user_id in user_ids:
            if me_id is not None and int(user_id) == int(me_id):
                continue

            user_name = str(user_id)
            try:
                user_name = await self._resolve_entity_name_from_client(event.client, int(user_id))
            except Exception:
                pass
            added_by = getattr(event, "added_by", None)
            inviter_id = added_by if isinstance(added_by, int) else getattr(added_by, "id", None)

            join_event = MemberJoinEvent(
                user_id=int(user_id),
                user_name=user_name,
                chat_id=int(chat_id),
                inviter_id=inviter_id,
            )
            try:
                async with get_db_session() as db:
                    handler = AcquisitionEventHandler(db=db, account_pool=self._account_pool)
                    await handler.initialize(**self._growth_handler_init_options)
                    await handler.on_member_joined(join_event)
            except Exception as exc:
                logger.warning(
                    "growth_member_join_dispatch_failed",
                    account_id=account_id,
                    chat_id=chat_id,
                    user_id=user_id,
                    error=str(exc),
                )

    @staticmethod
    def _event_flag(event: Any, name: str) -> bool:
        value = getattr(event, name, False)
        if callable(value):
            try:
                return bool(value())
            except TypeError:
                return False
        return bool(value)

    async def _run_guardian_bot_cycle(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        async with get_db_session() as db:
            profiles = (
                await db.execute(
                    select(GuardianBotProfile)
                    .join(TelegramAccount, GuardianBotProfile.account_id == TelegramAccount.id)
                    .where(TelegramAccount.account_type == AccountType.GUARDIAN_BOT)
                    .where(TelegramAccount.is_active == True)
                    .where(GuardianBotProfile.enabled == True)
                )
            ).scalars().all()

        processed_updates = 0
        failed_bots = 0
        discovered_groups = 0
        synced_groups = 0
        group_sync_errors = 0
        errors: list[dict[str, Any]] = []
        owned_governance = await self._guardian_owned_asset_counts()
        owned_governance.update(
            {
                "processed_updates": 0,
                "skipped_unbound_updates": 0,
                "skipped_mismatched_bot_updates": 0,
                "skipped_inactive_updates": 0,
                "skipped_gate_updates": 0,
                "skipped_not_managed_updates": 0,
                "permission_degraded": 0,
            }
        )

        for profile in profiles:
            client = TelegramClient(TelegramConfig(bot_token=profile.bot_token, timeout=min(self.heartbeat_interval, 30)))
            try:
                profile_group_sync_errors = 0
                bot_user = await client.get_me()
                await self._assert_guardian_runtime_identity(
                    profile, int(bot_user.user_id)
                )
                offset = self._guardian_update_offsets.get(profile.id)
                updates = await client.get_updates(offset=offset, limit=50, timeout=0)
                if updates:
                    self._guardian_update_offsets[profile.id] = max(update["update_id"] for update in updates) + 1
                    chat_payloads = self._guardian_chat_payloads_from_updates(updates)
                    discovered_groups += len(chat_payloads)
                    for chat_payload in chat_payloads:
                        try:
                            if await self._sync_guardian_group_from_chat(
                                profile,
                                client,
                                bot_user.user_id,
                                chat_payload,
                                governance_metrics=owned_governance,
                            ):
                                synced_groups += 1
                        except Exception:
                            group_sync_errors += 1
                            profile_group_sync_errors += 1
                            errors.append(
                                {
                                    "bot_account_id": profile.account_id,
                                    "telegram_chat_id": chat_payload.get("id"),
                                    "reason_code": "guardian_group_sync_failed",
                                }
                            )
                            logger.warning(
                                "guardian_group_sync_failed",
                                bot_account_id=profile.account_id,
                                telegram_chat_id=chat_payload.get("id"),
                                reason_code="guardian_group_sync_failed",
                            )
                    processed_updates += await self._dispatch_guardian_updates(
                        profile.account_id,
                        client,
                        updates,
                        governance_metrics=owned_governance,
                    )
                active_bindings = await self._guardian_active_binding_count(profile.account_id)
                await self._mark_guardian_profile(
                    profile.id,
                    GuardianBotHealthStatus.HEALTHY if active_bindings else GuardianBotHealthStatus.DEGRADED,
                    sync_status="partial" if profile_group_sync_errors else "synced",
                    last_synced_at=datetime.utcnow(),
                    bot_username=bot_user.username,
                    bot_user_id=bot_user.user_id,
                )
            except Exception:
                failed_bots += 1
                errors.append(
                    {
                        "bot_account_id": profile.account_id,
                        "reason_code": "guardian_bot_cycle_failed",
                    }
                )
                await self._mark_guardian_profile(profile.id, GuardianBotHealthStatus.DEGRADED, sync_status="failed")
                logger.warning(
                    "guardian_bot_cycle_failed",
                    bot_account_id=profile.account_id,
                    reason_code="guardian_bot_cycle_failed",
                )
            finally:
                await client.close()

        # Permission loss may have changed asset states during this cycle, so
        # report final counts rather than the pre-poll snapshot.
        owned_governance.update(await self._guardian_owned_asset_counts())

        return {
            "runtime": {
                "bot_api_polling": True,
                "processed_updates": processed_updates,
                "failed_bots": failed_bots,
                "discovered_groups": discovered_groups,
                "synced_groups": synced_groups,
                "group_sync_errors": group_sync_errors,
                "errors": errors[:5],
            },
            "owned_group_governance": owned_governance,
        }

    async def _assert_guardian_runtime_identity(
        self, profile: GuardianBotProfile, runtime_bot_user_id: int
    ) -> None:
        """Reject a token whose getMe identity conflicts with either profile."""

        async with get_db_session() as db:
            current_profile = await db.get(GuardianBotProfile, profile.id)
            account = await db.get(TelegramAccount, profile.account_id)
            owned_profiles = (
                await db.scalars(
                    select(OwnedBotProfile).where(
                        OwnedBotProfile.account_id == profile.account_id,
                        OwnedBotProfile.enabled == True,
                        OwnedBotProfile.status == "verified",
                    )
                )
            ).all()
            if (
                current_profile is None
                or not bool(current_profile.enabled)
                or account is None
                or not bool(account.is_active)
            ):
                raise RuntimeError("guardian_bot_profile_invalid")
            if len(owned_profiles) > 1:
                raise RuntimeError("owned_bot_profile_ambiguous")
            expected_ids = [current_profile.bot_user_id]
            if owned_profiles:
                expected_ids.append(owned_profiles[0].bot_user_id)
            if any(
                expected_id is not None
                and int(expected_id) != int(runtime_bot_user_id)
                for expected_id in expected_ids
            ):
                raise RuntimeError("guardian_bot_identity_mismatch")

    async def _guardian_owned_asset_counts(self) -> dict[str, int]:
        async with get_db_session() as db:
            rows = await db.execute(
                select(OwnedGroupAsset.governance_status, func.count(OwnedGroupAsset.id))
                .where(OwnedGroupAsset.governance_status.in_(["managed", "degraded"]))
                .group_by(OwnedGroupAsset.governance_status)
            )
        counts = {str(status): int(count) for status, count in rows.all()}
        return {
            "managed_assets": counts.get("managed", 0),
            "degraded_assets": counts.get("degraded", 0),
        }

    @staticmethod
    def _is_guardian_managed_chat(chat: dict[str, Any]) -> bool:
        return chat.get("type") in {"group", "supergroup", "channel"} and chat.get("id") is not None

    @classmethod
    def _guardian_chat_payloads_from_updates(cls, updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        chats: dict[int, dict[str, Any]] = {}
        for update in updates:
            for key in ("message", "edited_message", "channel_post", "edited_channel_post"):
                message = update.get(key) or {}
                chat = message.get("chat") or {}
                if cls._is_guardian_managed_chat(chat):
                    chats[int(chat["id"])] = chat

            for key in ("my_chat_member", "chat_member"):
                membership_update = update.get(key) or {}
                chat = membership_update.get("chat") or {}
                if cls._is_guardian_managed_chat(chat):
                    chats[int(chat["id"])] = chat
        return list(chats.values())

    @staticmethod
    def _guardian_role_and_status(member: dict[str, Any]) -> tuple[ManagedGroupBotRole, ManagedGroupBindingStatus]:
        return guardian_role_and_status_from_member(member)

    async def _sync_guardian_group_from_chat(
        self,
        profile: GuardianBotProfile,
        client: TelegramClient,
        bot_user_id: int,
        chat_payload: dict[str, Any],
        *,
        governance_metrics: dict[str, int] | None = None,
    ) -> bool:
        chat_id = int(chat_payload["id"])
        chat_title = chat_payload.get("title")
        chat_username = chat_payload.get("username")
        chat_type = chat_payload.get("type")
        member_count: Optional[int] = None

        # A self-owned chat is never auto-bound. It must already satisfy every
        # explicit asset/binding invariant before this worker calls Telegram.
        async with get_db_session() as db:
            preliminary_target = await resolve_governance_worker_target(
                db,
                telegram_chat_id=chat_id,
                bot_account_id=profile.account_id,
            )
            if preliminary_target.is_owned_group:
                gate_reason = await owned_group_governance_gate_reason()
                preliminary_target = await resolve_governance_worker_target(
                    db,
                    telegram_chat_id=chat_id,
                    bot_account_id=profile.account_id,
                    owned_gate_reason=gate_reason,
                )
            if preliminary_target.is_owned_group and not preliminary_target.allowed:
                add_owned_governance_audit(
                    db,
                    preliminary_target,
                    event_type="owned_group_governance_update_skipped",
                    result="skipped",
                    reason_code=preliminary_target.reason_code,
                    correlation_id=(
                        f"guardian-sync:{profile.account_id}:{chat_id}"
                    ),
                )
        if preliminary_target.is_owned_group and not preliminary_target.allowed:
            logger.info(
                "guardian_owned_group_sync_skipped",
                asset_id=preliminary_target.owned_group_asset_id,
                core_group_id=preliminary_target.core_group_id,
                telegram_chat_id=chat_id,
                managed_binding_id=preliminary_target.managed_binding_id,
                bot_account_id=profile.account_id,
                reason_code=preliminary_target.reason_code,
            )
            return False

        try:
            chat_info = await client.get_chat(chat_id)
            chat_title = chat_info.title or chat_title
            chat_username = chat_info.username if chat_info.username is not None else chat_username
            chat_type = chat_info.type or chat_type
            member_count = chat_info.member_count
        except Exception:
            logger.debug(
                "guardian_get_chat_failed",
                asset_id=preliminary_target.owned_group_asset_id,
                core_group_id=preliminary_target.core_group_id,
                telegram_chat_id=chat_id,
                managed_binding_id=preliminary_target.managed_binding_id,
                bot_account_id=profile.account_id,
                reason_code="guardian_get_chat_failed",
            )

        if member_count is None:
            try:
                member_count = await client.get_chat_member_count(chat_id)
            except Exception:
                logger.debug(
                    "guardian_get_chat_member_count_failed",
                    asset_id=preliminary_target.owned_group_asset_id,
                    core_group_id=preliminary_target.core_group_id,
                    telegram_chat_id=chat_id,
                    managed_binding_id=preliminary_target.managed_binding_id,
                    bot_account_id=profile.account_id,
                    reason_code="guardian_get_chat_member_count_failed",
                )

        member: dict[str, Any]
        member_probe_failed = False
        try:
            member = await client.get_chat_member(chat_id, bot_user_id)
        except Exception:
            member = {"status": "unknown"}
            member_probe_failed = True

        member_evaluation = evaluate_guardian_member(
            member,
            chat_type=chat_type,
            bot_user_id=bot_user_id,
            probe_failed=member_probe_failed,
        )
        try:
            async with get_db_session() as db:
                await acquire_telegram_chat_transaction_lock(db, chat_id)
                current_target = await resolve_governance_worker_target(
                    db,
                    telegram_chat_id=chat_id,
                    bot_account_id=profile.account_id,
                )
                if current_target.is_owned_group:
                    # The Telegram permission probe can take long enough for an
                    # operator to engage the runtime stop. Sample again inside
                    # the final chat-locked transaction before any DB sync.
                    gate_reason = await owned_group_governance_gate_reason()
                    current_target = await resolve_governance_worker_target(
                        db,
                        telegram_chat_id=chat_id,
                        bot_account_id=profile.account_id,
                        owned_gate_reason=gate_reason,
                    )
                if current_target.is_owned_group and not current_target.allowed:
                    add_owned_governance_audit(
                        db,
                        current_target,
                        event_type="owned_group_governance_update_skipped",
                        result="skipped",
                        reason_code=current_target.reason_code,
                        correlation_id=(
                            f"guardian-sync:{profile.account_id}:{chat_id}"
                        ),
                    )
                    logger.info(
                        "guardian_owned_group_sync_skipped_after_probe",
                        asset_id=current_target.owned_group_asset_id,
                        core_group_id=current_target.core_group_id,
                        telegram_chat_id=chat_id,
                        managed_binding_id=current_target.managed_binding_id,
                        bot_account_id=profile.account_id,
                        reason_code=current_target.reason_code,
                    )
                    return False

                if current_target.is_owned_group:
                    bot_role = member_evaluation.bot_role
                    binding_status = member_evaluation.binding_status
                else:
                    # Legacy managed groups keep their current role-based
                    # activation semantics. Only the persisted snapshot is
                    # narrowed to safe fields.
                    bot_role, binding_status = self._guardian_role_and_status(member)

                await sync_managed_group_binding(
                    db,
                    bot_account_id=profile.account_id,
                    telegram_group_id=chat_id,
                    group_id=(
                        current_target.core_group_id
                        if current_target.allowed
                        else None
                    ),
                    title=chat_title,
                    username=chat_username,
                    member_count=member_count,
                    binding_status=binding_status,
                    bot_role=bot_role,
                    permissions_snapshot=member_evaluation.snapshot,
                    replace_permissions_snapshot=current_target.is_owned_group,
                    discovery_source="guardian_auto_sync",
                    allow_existing=True,
                    reject_owned_group_auto_bind=(
                        not current_target.is_owned_group
                    ),
                )
                if current_target.is_owned_group:
                    asset = await db.get(
                        OwnedGroupAsset, current_target.owned_group_asset_id
                    )
                    if asset is not None:
                        asset.governance_last_checked_at = datetime.utcnow()
                        if not member_evaluation.passed:
                            before_state = owned_governance_audit_state(
                                current_target,
                                permission_snapshot=member_evaluation.snapshot,
                            )
                            asset.governance_status = "degraded"
                            asset.governance_last_error_code = (
                                member_evaluation.reason_code
                            )
                            asset.governance_last_error_message = (
                                "Guardian Bot permissions or membership changed"
                            )
                            if governance_metrics is not None:
                                governance_metrics["permission_degraded"] += 1
                            add_owned_governance_audit(
                                db,
                                current_target,
                                event_type="owned_group_governance_degraded",
                                result="failed",
                                reason_code=(
                                    member_evaluation.reason_code
                                    or "guardian_permissions_missing"
                                ),
                                correlation_id=(
                                    f"guardian-permission:{profile.account_id}:{chat_id}"
                                ),
                                before_state=before_state,
                                permission_snapshot=member_evaluation.snapshot,
                            )
                            logger.warning(
                                "guardian_owned_group_permission_degraded",
                                asset_id=asset.id,
                                core_group_id=asset.core_group_id,
                                telegram_chat_id=chat_id,
                                managed_binding_id=asset.managed_binding_id,
                                bot_account_id=profile.account_id,
                                reason_code=member_evaluation.reason_code,
                            )
        except ManagedGroupSyncConflict:
            logger.info(
                "guardian_group_sync_conflict",
                asset_id=preliminary_target.owned_group_asset_id,
                core_group_id=preliminary_target.core_group_id,
                telegram_chat_id=chat_id,
                managed_binding_id=preliminary_target.managed_binding_id,
                bot_account_id=profile.account_id,
                reason_code="managed_group_sync_conflict",
            )
            return False
        return True

    async def _guardian_active_binding_count(self, bot_account_id: int) -> int:
        async with get_db_session() as db:
            return (
                await db.execute(
                    select(func.count(ManagedGroupBinding.id)).where(
                        ManagedGroupBinding.bot_account_id == bot_account_id,
                        ManagedGroupBinding.binding_status == ManagedGroupBindingStatus.ACTIVE,
                    )
                )
            ).scalar() or 0

    async def _dispatch_guardian_updates(
        self,
        bot_account_id: int,
        telegram_client: TelegramClient,
        updates: list[dict[str, Any]],
        *,
        governance_metrics: dict[str, int] | None = None,
    ) -> int:
        processed = 0
        async with get_db_session() as db:
            telegram_client.risk_guard = AccountRiskGuard(db)
            telegram_client.risk_account = bot_risk_identity(
                f"guardian_worker:{bot_account_id}"
            )
            bot = await create_guardian_bot(db, telegram_client=telegram_client)
            for update in updates:
                update_kind: Literal["message", "edited_message"]
                message = update.get("message")
                if isinstance(message, dict):
                    update_kind = "message"
                else:
                    message = update.get("edited_message")
                    if not isinstance(message, dict):
                        continue
                    update_kind = "edited_message"
                if not message:
                    continue
                processed += await self._dispatch_guardian_message(
                    bot,
                    telegram_client,
                    message,
                    db=db,
                    bot_account_id=bot_account_id,
                    update_id=update.get("update_id"),
                    update_kind=update_kind,
                    governance_metrics=governance_metrics,
                )
            await bot.cleanup()
        logger.info(
            "guardian_updates_dispatched",
            bot_account_id=bot_account_id,
            processed=processed,
        )
        return processed

    async def _dispatch_guardian_message(
        self,
        bot: Any,
        telegram_client: TelegramClient,
        message: dict[str, Any],
        *,
        db: AsyncSession,
        bot_account_id: int,
        update_id: int,
        update_kind: Literal["message", "edited_message"],
        governance_metrics: dict[str, int] | None = None,
    ) -> int:
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = chat.get("id")
        user_id = sender.get("id")
        if chat_id is None:
            return 0

        is_private = chat.get("type") == "private" or (
            user_id is not None and int(chat_id) == int(user_id)
        )
        core_group_id: int | None = None
        target = None
        if not is_private:
            target = await resolve_governance_worker_target(
                db,
                telegram_chat_id=int(chat_id),
                bot_account_id=bot_account_id,
            )
            if target.is_owned_group:
                # Polling-cycle snapshots are deliberately insufficient here:
                # every owned update samples the stop immediately before its
                # moderation handlers are allowed to run.
                gate_reason = await owned_group_governance_gate_reason()
                target = await resolve_governance_worker_target(
                    db,
                    telegram_chat_id=int(chat_id),
                    bot_account_id=bot_account_id,
                    owned_gate_reason=gate_reason,
                )
            if not target.allowed:
                if governance_metrics is not None and target.is_owned_group:
                    if target.reason_code in {
                        "owned_group_binding_missing",
                        "managed_binding_missing",
                    }:
                        governance_metrics["skipped_unbound_updates"] += 1
                    elif target.reason_code == "mismatched_guardian_bot":
                        governance_metrics["skipped_mismatched_bot_updates"] += 1
                    elif target.reason_code == "managed_binding_not_active":
                        governance_metrics["skipped_inactive_updates"] += 1
                    elif target.reason_code in {
                        "governance_feature_disabled",
                        "governance_stop_enabled",
                        "governance_gate_backend_unavailable",
                    }:
                        governance_metrics["skipped_gate_updates"] += 1
                    else:
                        governance_metrics["skipped_not_managed_updates"] += 1
                if target.is_owned_group:
                    add_owned_governance_audit(
                        db,
                        target,
                        event_type="owned_group_governance_update_skipped",
                        result="skipped",
                        reason_code=target.reason_code,
                        correlation_id=(
                            f"guardian-update:{bot_account_id}:{chat_id}:"
                            f"{update_id}"
                        ),
                    )
                logger.info(
                    "guardian_group_update_skipped",
                    asset_id=target.owned_group_asset_id,
                    core_group_id=target.core_group_id,
                    telegram_chat_id=target.telegram_chat_id,
                    managed_binding_id=target.managed_binding_id,
                    bot_account_id=bot_account_id,
                    reason_code=target.reason_code,
                )
                return 0
            core_group_id = target.core_group_id
            if governance_metrics is not None and target.is_owned_group:
                governance_metrics["processed_updates"] += 1

            if target.is_owned_group and target.owned_group_asset_id is not None:
                try:
                    # The projector opens and commits its own short session. It
                    # must finish before Guardian handlers run so their later
                    # failures cannot roll back an accepted member fact.
                    await project_owned_group_member_observations(
                        message,
                        group_asset_id=target.owned_group_asset_id,
                        core_group_id=target.core_group_id,
                        source_bot_account_id=bot_account_id,
                        update_id=update_id,
                        update_kind=update_kind,
                    )
                except Exception:
                    # The projector is itself fail-open; this final boundary
                    # also protects Guardian when a test double or future
                    # telemetry adapter violates that contract.
                    logger.warning(
                        "owned_group_member_observation_failed",
                        asset_id=target.owned_group_asset_id,
                        core_group_id=target.core_group_id,
                        bot_account_id=bot_account_id,
                        update_id=(update_id if type(update_id) is int else None),
                        event_type="unknown",
                        result="failed",
                        reason_code="projector_unhandled_error",
                        candidate_count=0,
                    )

        processed = 0
        for member in message.get("new_chat_members") or []:
            member_id = member.get("id")
            if member_id is None:
                continue
            response = await bot.handle_new_member(
                chat_id=chat_id,
                user_id=member_id,
                username=member.get("username"),
                core_group_id=core_group_id,
            )
            if response:
                await telegram_client.send_message(chat_id, response)
            processed += 1

        left_member = message.get("left_chat_member")
        if left_member and left_member.get("id") is not None:
            await bot.handle_member_leave(
                chat_id=chat_id,
                user_id=left_member["id"],
                core_group_id=core_group_id,
            )
            processed += 1

        text = message.get("text") or message.get("caption") or ""
        if text and user_id is not None:
            await bot.handle_message(
                message_id=message.get("message_id", 0),
                chat_id=chat_id,
                user_id=user_id,
                username=sender.get("username"),
                text=text,
                core_group_id=core_group_id,
            )
            processed += 1

        return processed

    async def _mark_guardian_profile(
        self,
        profile_id: int,
        status: GuardianBotHealthStatus,
        *,
        sync_status: Optional[str] = None,
        last_synced_at: Optional[datetime] = None,
        bot_username: Optional[str] = None,
        bot_user_id: Optional[int] = None,
    ) -> None:
        async with get_db_session() as db:
            profile = await db.get(GuardianBotProfile, profile_id)
            if profile is None:
                return
            profile.health_status = status
            profile.last_heartbeat_at = datetime.utcnow()
            if sync_status is not None:
                profile.sync_status = sync_status
            if last_synced_at is not None:
                profile.last_synced_at = last_synced_at
            if bot_username:
                profile.bot_username = bot_username
            if bot_user_id:
                profile.bot_user_id = bot_user_id

    def _status_for_snapshot(self, snapshot: dict[str, Any]) -> str:
        runtime = snapshot.get("runtime") or {}
        if self.role == TelegramWorkerRole.GROWTH_USER:
            if snapshot.get("enabled_accounts", 0) <= 0:
                return TelegramWorkerStatusValue.DEGRADED.value
            if runtime.get("runtime_capable_accounts") == 0:
                return TelegramWorkerStatusValue.DEGRADED.value
            return TelegramWorkerStatusValue.ONLINE.value

        if snapshot.get("enabled_bots", 0) <= 0:
            return TelegramWorkerStatusValue.DEGRADED.value
        if runtime.get("failed_bots", 0) >= snapshot.get("enabled_bots", 0):
            return TelegramWorkerStatusValue.DEGRADED.value
        return TelegramWorkerStatusValue.ONLINE.value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Vanguard Telegram worker")
    parser.add_argument("--role", choices=[role.value for role in TelegramWorkerRole], required=True)
    parser.add_argument("--worker-id", default=None)
    parser.add_argument("--heartbeat-interval", type=int, default=30)
    parser.add_argument("--once", action="store_true", help="Run one configuration/runtime cycle and exit")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    worker = TelegramWorker(
        role=TelegramWorkerRole(args.role),
        worker_id=args.worker_id,
        heartbeat_interval=args.heartbeat_interval,
    )
    if args.once:
        await init_db(create_tables=not settings.is_production)
        await init_redis()
        try:
            await worker.run_once()
        finally:
            await close_redis()
            await close_db()
        return

    await worker.run()


if __name__ == "__main__":
    setup_logging()
    asyncio.run(main())
