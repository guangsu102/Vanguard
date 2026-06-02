"""Unified Telegram worker entrypoint.

This module establishes the runtime contract for the converged architecture:
growth user workers and guardian bot workers read backend configuration and
report heartbeat state back to the backend database.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
from datetime import datetime
from typing import Any, Optional

import structlog
from sqlalchemy import func, select
from telethon import events as telethon_events

from app.core.account.models import (
    AccountOperationConfig,
    AccountStatus,
    AccountType,
    GuardianBotHealthStatus,
    GuardianBotProfile,
    TelegramAccount,
)
from app.core.account.pool import AccountPool
from app.core.config import settings
from app.core.database import close_db, get_db_session, init_db
from app.core.redis import close_redis, init_redis
from app.core.worker_status import TelegramWorkerRole, TelegramWorkerStatus, TelegramWorkerStatusValue
from app.integrations.telegram.client import TelegramClient, TelegramConfig
from app.modules.acquisition.handler import AcquisitionEventHandler, MemberJoinEvent, MessageEvent
from app.modules.acquisition.models import AdCampaign, GroupSearchKeyword, KeywordTrigger, MessageTemplate, SearchKeywordStatus
from app.modules.guardian.main import create_guardian_bot
from app.modules.guardian.models import ManagedGroupBinding, ManagedGroupBindingStatus, ModerationRule, ModerationSensitiveKeyword

logger = structlog.get_logger()


class TelegramWorker:
    def __init__(self, role: TelegramWorkerRole, worker_id: Optional[str] = None, heartbeat_interval: int = 30):
        self.role = role
        self.worker_id = worker_id or f"{role.value}:{socket.gethostname()}"
        self.heartbeat_interval = heartbeat_interval
        self._running = False
        self._account_pool: Optional[AccountPool] = AccountPool() if role == TelegramWorkerRole.GROWTH_USER else None
        self._guardian_update_offsets: dict[int, int] = {}
        self._growth_listener_sessions: dict[int, str] = {}

    async def run(self) -> None:
        await init_db(create_tables=not settings.is_production)
        await init_redis()
        self._running = True
        try:
            await self._heartbeat(TelegramWorkerStatusValue.STARTING.value, {"phase": "startup"})
            while self._running:
                await self.run_once()
                await asyncio.sleep(self.heartbeat_interval)
        except asyncio.CancelledError:
            await self._heartbeat(TelegramWorkerStatusValue.OFFLINE.value, {"phase": "cancelled"})
            raise
        except Exception as exc:
            logger.exception("telegram_worker_failed", worker_id=self.worker_id, role=self.role.value, error=str(exc))
            await self._heartbeat(TelegramWorkerStatusValue.ERROR.value, {"phase": "error"}, last_error=str(exc))
            raise
        finally:
            if self._account_pool is not None:
                await self._account_pool.close_all()
            await close_redis()
            await close_db()

    async def stop(self) -> None:
        self._running = False
        await self._heartbeat(TelegramWorkerStatusValue.OFFLINE.value, {"phase": "shutdown"})

    async def run_once(self) -> dict[str, Any]:
        snapshot = await self._load_configuration_snapshot()
        cycle_metadata = await self._run_role_cycle(snapshot)
        snapshot.update(cycle_metadata)
        status = self._status_for_snapshot(snapshot)
        await self._heartbeat(status, snapshot)
        return {"status": status, "metadata": snapshot}

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
                        .join(AccountOperationConfig, AccountOperationConfig.account_id == TelegramAccount.id)
                        .where(TelegramAccount.account_type == AccountType.PROMOTER)
                        .where(TelegramAccount.is_active == True)
                        .where(TelegramAccount.status.notin_([AccountStatus.ERROR, AccountStatus.BANNED]))
                        .where(AccountOperationConfig.enabled == True)
                    )
                ).scalars().all()

        runtime_accounts = [
            account
            for account in accounts
            if account.phone and account.api_config and account.api_config.api_id and account.api_config.api_hash
        ]
        synced = await self._account_pool.sync_from_db(runtime_accounts)
        listener_state = await self._ensure_growth_listeners(runtime_accounts)
        pool_stats = await self._account_pool.health_check()
        return {
            "runtime": {
                "account_pool_size": self._account_pool.size,
                "account_pool_synced": synced,
                "runtime_capable_accounts": len(runtime_accounts),
                "runtime_skipped_accounts": len(accounts) - len(runtime_accounts),
                **listener_state,
                "account_pool": pool_stats,
                "listener": "telethon_user_listener",
            }
        }

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
            await self._handle_growth_new_message(account_id, event)

        async def handle_chat_action(event: Any) -> None:
            await self._handle_growth_chat_action(account_id, event)

        client.add_event_handler(handle_new_message, telethon_events.NewMessage(incoming=True))
        client.add_event_handler(handle_chat_action, telethon_events.ChatAction())
        logger.info("growth_event_handlers_attached", account_id=account_id, session_name=account.session_name)

    async def _handle_growth_new_message(self, account_id: int, event: Any) -> None:
        text = getattr(event, "raw_text", None) or getattr(event, "text", None) or ""
        if not text:
            return

        sender_id = getattr(event, "sender_id", None)
        chat_id = getattr(event, "chat_id", None)
        message_id = getattr(event, "id", None) or getattr(getattr(event, "message", None), "id", 0)
        if sender_id is None or chat_id is None:
            return

        sender_name = str(sender_id)
        try:
            sender = await event.get_sender()
            sender_name = (
                getattr(sender, "username", None)
                or getattr(sender, "first_name", None)
                or getattr(sender, "title", None)
                or sender_name
            )
        except Exception:
            pass

        is_private = bool(getattr(event, "is_private", False))
        message_event = MessageEvent(
            message_id=int(message_id or 0),
            chat_id=int(chat_id),
            sender_id=int(sender_id),
            sender_name=sender_name,
            content=text,
            is_group=not is_private,
            timestamp=datetime.utcnow(),
        )

        try:
            async with get_db_session() as db:
                handler = AcquisitionEventHandler(db=db, account_pool=self._account_pool)
                await handler.initialize()
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
                user = await event.client.get_entity(user_id)
                user_name = getattr(user, "username", None) or getattr(user, "first_name", None) or user_name
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
                    await handler.initialize()
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
        errors: list[dict[str, Any]] = []
        active_bindings = snapshot.get("active_group_bindings", 0)

        for profile in profiles:
            client = TelegramClient(TelegramConfig(bot_token=profile.bot_token, timeout=min(self.heartbeat_interval, 30)))
            try:
                offset = self._guardian_update_offsets.get(profile.id)
                updates = await client.get_updates(offset=offset, limit=50, timeout=0)
                if updates:
                    self._guardian_update_offsets[profile.id] = max(update["update_id"] for update in updates) + 1
                    processed_updates += await self._dispatch_guardian_updates(profile.id, client, updates)
                await self._mark_guardian_profile(
                    profile.id,
                    GuardianBotHealthStatus.HEALTHY if active_bindings else GuardianBotHealthStatus.DEGRADED,
                )
            except Exception as exc:
                failed_bots += 1
                errors.append({"bot_profile_id": profile.id, "error": str(exc)})
                await self._mark_guardian_profile(profile.id, GuardianBotHealthStatus.DEGRADED)
                logger.warning("guardian_bot_cycle_failed", bot_profile_id=profile.id, error=str(exc))
            finally:
                await client.close()

        return {
            "runtime": {
                "bot_api_polling": True,
                "processed_updates": processed_updates,
                "failed_bots": failed_bots,
                "errors": errors[:5],
            }
        }

    async def _dispatch_guardian_updates(
        self,
        bot_profile_id: int,
        telegram_client: TelegramClient,
        updates: list[dict[str, Any]],
    ) -> int:
        processed = 0
        async with get_db_session() as db:
            bot = await create_guardian_bot(db, telegram_client=telegram_client)
            for update in updates:
                message = update.get("message") or update.get("edited_message")
                if not message:
                    continue
                processed += await self._dispatch_guardian_message(bot, telegram_client, message)
            await bot.cleanup()
        logger.info("guardian_updates_dispatched", bot_profile_id=bot_profile_id, processed=processed)
        return processed

    async def _dispatch_guardian_message(self, bot: Any, telegram_client: TelegramClient, message: dict[str, Any]) -> int:
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = chat.get("id")
        user_id = sender.get("id")
        if chat_id is None:
            return 0

        processed = 0
        for member in message.get("new_chat_members") or []:
            member_id = member.get("id")
            if member_id is None:
                continue
            response = await bot.handle_new_member(
                chat_id=chat_id,
                user_id=member_id,
                username=member.get("username"),
            )
            if response:
                await telegram_client.send_message(chat_id, response)
            processed += 1

        left_member = message.get("left_chat_member")
        if left_member and left_member.get("id") is not None:
            await bot.handle_member_leave(chat_id=chat_id, user_id=left_member["id"])
            processed += 1

        text = message.get("text") or message.get("caption") or ""
        if text and user_id is not None:
            await bot.handle_message(
                message_id=message.get("message_id", 0),
                chat_id=chat_id,
                user_id=user_id,
                username=sender.get("username"),
                text=text,
            )
            processed += 1

        return processed

    async def _mark_guardian_profile(self, profile_id: int, status: GuardianBotHealthStatus) -> None:
        async with get_db_session() as db:
            profile = await db.get(GuardianBotProfile, profile_id)
            if profile is None:
                return
            profile.health_status = status
            profile.last_heartbeat_at = datetime.utcnow()

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
    asyncio.run(main())
