"""Telegram execution helpers.

This module keeps Telegram client calls behind a narrow execution boundary so
business workflows can ask for an action without reimplementing the send/join
plumbing every time.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import parse_qs, urlparse

import structlog

from app.core.account.risk_guard import AccountRiskAction, AccountRiskGuard
from app.core.account.system_identity import bot_risk_identity
from app.core.group.identity import is_owned_group_target
from app.modules.owned_group.security import safe_exception_message


class TelegramExecutionError(RuntimeError):
    """Raised when a Telegram operation cannot be executed."""

    def __init__(self, message: str, *, retry_after_seconds: Optional[int] = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class TelegramSendOutcomeUnknownError(TelegramExecutionError):
    """A Telegram write may have occurred but its outcome cannot be confirmed."""


class TelegramSendPreflightError(TelegramExecutionError):
    """A confirmed pre-send failure where no Telegram write was attempted."""


class TelegramSendReservationReleasePendingError(TelegramSendPreflightError):
    """The write was not attempted, but its risk reservation still needs recovery."""


class TelegramJoinRequestPendingError(TelegramExecutionError):
    """Raised when Telegram accepted a join request that still needs approval."""


_BOTFATHER_TOKEN_RE = re.compile(r"\b(\d{2,14}:[A-Za-z0-9_-]{30,})\b")


def _extract_botfather_token(text: str) -> Optional[str]:
    match = _BOTFATHER_TOKEN_RE.search(str(text or ""))
    return match.group(1) if match else None


@dataclass(frozen=True)
class ParsedTelegramGroupLink:
    """Validated Telegram group link without any unrelated URL components."""

    kind: str
    target: str


_TELEGRAM_LINK_HOSTS = {"t.me", "telegram.me"}
_OFFICIAL_SPAMBOT_ID = 178220800
logger = structlog.get_logger()
_RESERVED_PUBLIC_PATHS = {
    "addstickers",
    "addtheme",
    "boost",
    "c",
    "confirmphone",
    "contact",
    "giftcode",
    "invoice",
    "iv",
    "joinchat",
    "login",
    "m",
    "proxy",
    "s",
    "setlanguage",
    "share",
    "socks",
}


def parse_telegram_group_link(value: str) -> ParsedTelegramGroupLink:
    """Parse public group URLs and private Telegram invite links."""
    raw = str(value or "").strip()
    if not raw:
        raise TelegramExecutionError("Telegram group link is required")

    if raw.startswith("@"):
        username = raw[1:]
        if not _is_valid_public_username(username):
            raise TelegramExecutionError("invalid Telegram public group username")
        return ParsedTelegramGroupLink(kind="public", target=username)

    if raw.lower().startswith(("t.me/", "telegram.me/", "www.t.me/", "www.telegram.me/")):
        raw = f"https://{raw}"

    parsed = urlparse(raw)
    if parsed.scheme.lower() == "tg":
        if parsed.netloc.lower() != "join":
            raise TelegramExecutionError("unsupported Telegram group link")
        invite_hash = (parse_qs(parsed.query).get("invite") or [""])[0]
        return ParsedTelegramGroupLink(
            kind="private",
            target=_validate_invite_hash(invite_hash),
        )

    if parsed.scheme.lower() not in {"http", "https"}:
        raise TelegramExecutionError("invalid Telegram group link")
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in _TELEGRAM_LINK_HOSTS or parsed.username or parsed.password or parsed.port:
        raise TelegramExecutionError("only t.me or telegram.me group links are supported")

    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) == 1 and path_parts[0].startswith("+"):
        return ParsedTelegramGroupLink(
            kind="private",
            target=_validate_invite_hash(path_parts[0][1:]),
        )
    if len(path_parts) == 2 and path_parts[0].lower() == "joinchat":
        return ParsedTelegramGroupLink(
            kind="private",
            target=_validate_invite_hash(path_parts[1]),
        )
    if len(path_parts) != 1:
        raise TelegramExecutionError("link must point directly to a Telegram group")

    username = path_parts[0]
    if username.lower() in _RESERVED_PUBLIC_PATHS or not _is_valid_public_username(username):
        raise TelegramExecutionError("invalid Telegram public group link")
    return ParsedTelegramGroupLink(kind="public", target=username)


def _is_valid_public_username(value: str) -> bool:
    return (
        4 <= len(value) <= 32
        and value[0].isalpha()
        and all(char.isascii() and (char.isalnum() or char == "_") for char in value)
    )


def _normalize_managed_bot_username(value: str) -> str:
    username = str(value or "").strip().removeprefix("@")
    if (
        not 5 <= len(username) <= 32
        or not username.casefold().endswith("bot")
        or not all(char.isascii() and (char.isalnum() or char == "_") for char in username)
    ):
        raise TelegramExecutionError("invalid managed bot username")
    return username


def _normalize_managed_bot_name(value: str) -> str:
    name = str(value or "").strip()
    if not 1 <= len(name) <= 64:
        raise TelegramExecutionError("invalid managed bot name")
    return name


def _validate_invite_hash(value: str) -> str:
    invite_hash = str(value or "").strip()
    if not 8 <= len(invite_hash) <= 128 or not all(
        char.isascii() and (char.isalnum() or char in {"_", "-"}) for char in invite_hash
    ):
        raise TelegramExecutionError("invalid Telegram private invite link")
    return invite_hash


def _is_joinable_telegram_entity(entity: Any) -> bool:
    if entity is None:
        return False
    if getattr(entity, "broadcast", False) is True:
        return False
    if getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False):
        return True
    chat_type = str(getattr(entity, "type", "") or "").lower()
    if chat_type in {"group", "supergroup"}:
        return True
    return entity.__class__.__name__ in {"Chat", "ChatForbidden"}


class TelegramExecutionService:
    def __init__(self, risk_guard: Optional[AccountRiskGuard] = None):
        self.risk_guard = risk_guard
        self._bot_account = bot_risk_identity("telegram_execution")
        self.logger = logger.bind(module="telegram_execution")

    @staticmethod
    async def _notify_join_request_attempted(
        callback: Optional[Callable[[], Awaitable[None] | None]],
    ) -> None:
        if callback is None:
            return
        result = callback()
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _get_client(account: Any) -> Any:
        client = getattr(account, "client", None)
        if client is None and hasattr(account, "get_client"):
            client = account.get_client()
        if client is None and any(
            hasattr(account, attr)
            for attr in (
                "send_message",
                "send_file",
                "delete_message",
                "delete_messages",
                "pin_chat_message",
                "pin_message",
                "forward_messages",
                "restrict_chat_member",
                "ban_chat_member",
                "unban_chat_member",
                "send_reaction",
                "send_reaction_request",
                "get_entity",
            )
        ):
            client = account
        return client

    @asynccontextmanager
    async def _risk_operation(
        self,
        account: Any,
        action: AccountRiskAction,
        *,
        target_type: str,
        target_id: Any,
        details: Optional[dict[str, Any]] = None,
    ):
        if self.risk_guard is None:
            yield
            return

        try:
            decision = await self.risk_guard.check_and_reserve(
                account,
                action,
                target_type=target_type,
                target_id=target_id,
                details=details,
            )
        except BaseException as exc:
            if action != AccountRiskAction.OWNED_GROUP_MESSAGE:
                raise
            released = await self._release_owned_group_preflight_reservation(
                account,
                str((details or {}).get("risk_reservation_id") or ""),
            )
            if not isinstance(exc, Exception):
                raise
            if not released:
                raise TelegramSendReservationReleasePendingError(
                    "owned-group risk reservation release is pending"
                ) from exc
            raise TelegramSendPreflightError("owned-group risk preflight unavailable") from exc
        if not decision.allowed:
            raise TelegramExecutionError(
                f"risk_guard_blocked:{decision.reason}",
                retry_after_seconds=getattr(decision, "retry_after_seconds", None),
            )

        try:
            yield
        except TelegramSendPreflightError:
            if getattr(decision, "content_reservation", None):
                await self.risk_guard.release_content_reservation(decision.content_reservation)
            raise
        except Exception as exc:
            try:
                await self.risk_guard.record_failure(
                    account,
                    action,
                    exc,
                    target_type=target_type,
                    target_id=target_id,
                    details=details,
                )
            except Exception as audit_exc:
                if action not in {
                    AccountRiskAction.OWNED_GROUP_MESSAGE,
                    AccountRiskAction.MANAGED_BOT_CREATE,
                }:
                    raise
                await self._rollback_owned_risk_audit()
                event = (
                    "managed_bot_risk_failure_record_failed"
                    if action == AccountRiskAction.MANAGED_BOT_CREATE
                    else "owned_group_risk_failure_record_failed"
                )
                self.logger.error(
                    event,
                    error_type=type(audit_exc).__name__,
                    error=safe_exception_message(audit_exc, max_length=500),
                )
            raise
        else:
            try:
                await self.risk_guard.record_success(
                    account,
                    action,
                    target_type=target_type,
                    target_id=target_id,
                    details=details,
                )
            except Exception as audit_exc:
                if action not in {
                    AccountRiskAction.OWNED_GROUP_MESSAGE,
                    AccountRiskAction.MANAGED_BOT_CREATE,
                }:
                    raise
                await self._rollback_owned_risk_audit()
                event = (
                    "managed_bot_risk_success_record_failed"
                    if action == AccountRiskAction.MANAGED_BOT_CREATE
                    else "owned_group_risk_success_record_failed"
                )
                self.logger.error(
                    event,
                    error_type=type(audit_exc).__name__,
                    error=safe_exception_message(audit_exc, max_length=500),
                )

    async def _rollback_owned_risk_audit(self) -> None:
        db = getattr(self.risk_guard, "db", None)
        if db is None:
            return
        try:
            await db.rollback()
        except Exception:
            pass

    async def _release_owned_group_preflight_reservation(
        self,
        account: Any,
        reservation_id: str,
    ) -> bool:
        release = getattr(
            self.risk_guard,
            "release_owned_group_message_reservation",
            None,
        )
        if not callable(release) or not reservation_id:
            return True
        try:
            released = await release(account, reservation_id)
        except Exception as exc:
            self.logger.error(
                "owned_group_risk_preflight_release_failed",
                error_type=type(exc).__name__,
                error=safe_exception_message(exc, max_length=500),
            )
            return False
        if not bool(released):
            self.logger.error("owned_group_risk_preflight_release_unconfirmed")
            return False
        return True

    async def send_private_message(
        self,
        account: Any,
        user_id: int,
        message: str,
        *,
        initiated_by_user: bool = False,
        source: str = "unknown",
    ) -> bool:
        if self._get_client(account) is None:
            return False
        await self.send_private_message_result(
            account,
            user_id,
            message,
            initiated_by_user=initiated_by_user,
            source=source,
        )
        return True

    async def send_private_message_result(
        self,
        account: Any,
        user_id: int,
        message: str,
        *,
        initiated_by_user: bool = False,
        source: str = "unknown",
    ) -> Any:
        """Send a private message and return Telegram's message object."""
        client = self._get_client(account)
        if client is None:
            raise TelegramExecutionError("telegram client unavailable")

        if await self._outbound_service(account):
            raise TelegramSendPreflightError("dynamic_promoter_private_messages_disabled")

        async with self._risk_operation(
            account,
            AccountRiskAction.PRIVATE_MESSAGE,
            target_type="user",
            target_id=user_id,
            details={"source": source, "initiated_by_user": initiated_by_user, "content": message},
        ):
            return await client.send_message(user_id, message)

    async def send_group_message(
        self,
        account: Any,
        group_id: int,
        message: str,
        *,
        reply_to: Optional[int] = None,
        source: str = "unknown",
    ) -> Optional[int]:
        client = self._get_client(account)
        if client is None:
            return None

        if await self._outbound_service(account):
            raise TelegramSendPreflightError("dynamic_promoter_use_qualified_ad_or_verification")

        action = AccountRiskAction.GROUP_MESSAGE
        if source == "ad_probe":
            action = AccountRiskAction.AD_PROBE
        elif source.startswith("ad_") or source == "proactive_group_ai_warmup":
            action = AccountRiskAction.AI_WARMUP

        async with self._risk_operation(
            account,
            action,
            target_type="group",
            target_id=group_id,
            details={"source": source, "reply_to": reply_to, "content": message},
        ):
            result = await client.send_message(group_id, message, reply_to=reply_to)
            message_id = getattr(result, "id", getattr(result, "message_id", None))
            if message_id is None:
                raise RuntimeError("telegram send returned no message id")
        return message_id

    async def send_owned_group_message(
        self,
        account: Any,
        telegram_chat_id: int,
        message: str,
        *,
        reply_to: Optional[int] = None,
        execution_id: int,
        send_attempt_id: Optional[str] = None,
        before_telegram_write: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> Optional[int]:
        """Send one stage-2 owned-group message with its dedicated risk action.

        The caller must pass the already acquired, policy-selected account.
        Account selection and business quota checks intentionally stay outside
        this Telegram boundary. The risk reservation is therefore performed
        exactly once and immediately before the client write.
        """

        client = self._get_client(account)
        if client is None:
            raise TelegramSendPreflightError("telegram client unavailable")

        attempt_token = str(send_attempt_id or uuid.uuid4().hex)
        risk_reservation_id = AccountRiskGuard.owned_group_reservation_id(attempt_token)
        details = {
            "source": "owned_group_message",
            "execution_id": execution_id,
            "reply_to": reply_to,
            "risk_reservation_id": risk_reservation_id,
        }
        async with self._risk_operation(
            account,
            AccountRiskAction.OWNED_GROUP_MESSAGE,
            target_type="group",
            target_id=telegram_chat_id,
            details=details,
        ):
            if before_telegram_write is not None:
                try:
                    await before_telegram_write()
                except BaseException as exc:
                    released = await self._release_owned_group_preflight_reservation(
                        account,
                        details["risk_reservation_id"],
                    )
                    if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                        raise
                    if not released:
                        raise TelegramSendReservationReleasePendingError(
                            "owned-group risk reservation release is pending"
                        ) from exc
                    raise TelegramSendPreflightError(
                        "owned-group send marker could not be persisted"
                    ) from exc
            try:
                result = await client.send_message(
                    telegram_chat_id,
                    message,
                    reply_to=reply_to,
                )
            except Exception as exc:
                # Telegram RPC errors that explicitly reject the write retain
                # their original type so Speaker can apply the established
                # FloodWait/permanent-error policy. Transport and otherwise
                # unclassified failures may have reached Telegram, so retrying
                # them could duplicate a message.
                reason = AccountRiskGuard.classify_error(
                    exc,
                    action="owned_group_message",
                    target_type="group",
                )
                if reason in {
                    "flood_wait",
                    "peer_flood",
                    "account_banned",
                    "account_restricted",
                    "group_write_forbidden",
                }:
                    raise
                raise TelegramSendOutcomeUnknownError(
                    "Telegram send outcome could not be confirmed"
                ) from exc
            message_id = getattr(result, "id", None) or getattr(
                result,
                "message_id",
                None,
            )
            if message_id is None:
                raise TelegramSendOutcomeUnknownError("Telegram send returned without a message id")
        return int(message_id)

    async def _outbound_service(self, account: Any):
        if self.risk_guard is None:
            return None
        from sqlalchemy import select
        from app.core.account.models import AccountOperationConfig
        from app.core.account.outbound_budget import AccountOutboundBudgetService
        config = await self.risk_guard.db.scalar(select(AccountOperationConfig).where(
            AccountOperationConfig.account_id == account.account_id
        ).execution_options(populate_existing=True))
        if getattr(config, "dynamic_capacity_enabled", False) is True:
            return AccountOutboundBudgetService(self.risk_guard.db)
        return None

    async def _budgeted_write(
        self, account: Any, action: AccountRiskAction, *, target: Any,
        category: str, attempt_key: str, context: dict, details: dict,
        write: Callable[[], Awaitable[Any]], preflight: Callable[[], Awaitable[None]] | None = None,
        on_send_attempted: Callable[[], Any] | None = None,
        require_budget: bool = False,
        risk_target_type: str | None = None,
    ) -> Any:
        """Persist intent before RPC; an unknown write can only be reconciled."""
        from telethon.errors import RPCError
        from app.core.account.outbound_budget import OutboundBudgetBlocked
        budget = await self._outbound_service(account)
        if require_budget and budget is None:
            raise TelegramSendPreflightError("outbound_budget_required")
        if budget:
            if not attempt_key:
                raise TelegramSendPreflightError("outbound_idempotency_key_required")
            try:
                row = await budget.reserve(account.account_id, attempt_key=attempt_key,
                    category=category, target_key=str(target), context=context)
            except OutboundBudgetBlocked as exc:
                raise TelegramSendPreflightError(str(exc), retry_after_seconds=exc.retry_after_seconds) from exc
            if row.state != "reserved":
                unknown = TelegramSendOutcomeUnknownError("outbound_original_attempt_requires_reconciliation")
                unknown.telegram_message_id = row.message_id
                raise unknown
            details = {**details, "outbound_attempt_key": attempt_key}
        attempted, message_id, rpc_error = False, None, None
        try:
            async with self._risk_operation(account, action, target_type=risk_target_type or ("group" if category != "diagnostic" else "official_bot"),
                                            target_id=target, details=details):
                if preflight:
                    await preflight()
                if on_send_attempted is not None:
                    callback = on_send_attempted()
                    if inspect.isawaitable(callback):
                        await callback
                if budget:
                    await budget.mark_attempted(attempt_key, account_id=account.account_id)
                attempted = True
                try:
                    result = await write()
                except RPCError as exc:
                    rpc_error = exc
                    raise
                except Exception as exc:
                    rpc_error = TelegramSendOutcomeUnknownError("send_outcome_unknown:" + type(exc).__name__)
                    raise rpc_error from exc
                raw_id = getattr(result, "id", None) or getattr(result, "message_id", None)
                try:
                    message_id = int(raw_id) if raw_id is not None else None
                except (TypeError, ValueError):
                    message_id = None
                if not message_id or message_id <= 0:
                    rpc_error = TelegramSendOutcomeUnknownError("send_outcome_unknown:message_id_missing")
                    raise rpc_error
                if budget:
                    await budget.finish(attempt_key, account_id=account.account_id,
                                        state="succeeded", message_id=message_id)
            return result
        except Exception as exc:
            # Audit/database errors must not rewrite a known Telegram result into a retry.
            if budget:
                try:
                    await budget.db.rollback()
                    await budget.finish(attempt_key, account_id=account.account_id,
                        state="succeeded" if message_id else "failed" if isinstance(rpc_error, RPCError)
                              else "unknown" if attempted else "cancelled",
                        message_id=message_id, error_code=type(rpc_error or exc).__name__)
                except Exception:
                    pass  # attempted/reserved is durable and recovery remains responsible.
            if not attempted:
                if isinstance(exc, TelegramSendPreflightError):
                    raise
                raise TelegramSendPreflightError(str(exc), retry_after_seconds=getattr(exc, "retry_after_seconds", None)) from exc
            if message_id:
                unknown = TelegramSendOutcomeUnknownError("send_outcome_unknown:post_send_audit_failed:" + type(exc).__name__)
                unknown.telegram_message_id = message_id
                raise unknown from exc
            if rpc_error is not None:
                raise rpc_error from exc
            raise TelegramSendOutcomeUnknownError("send_outcome_unknown:" + type(exc).__name__) from exc

    async def send_ad(
        self, account: Any, target: int | str, content: str, *,
        media_url: Optional[str] = None, source: str = "acquisition_ad",
        delivery_policy: str = "growth", on_send_attempted: Optional[Callable[[], Any]] = None,
        reservation_token: Optional[str] = None,
    ) -> Optional[int]:
        client = self._get_client(account)
        if client is None or self.risk_guard is None:
            raise TelegramSendPreflightError("qualification_execution_unavailable")
        from app.modules.acquisition.qualification_service import current_authorization, send_gate
        from app.modules.acquisition.qualification_actions import validate_live_send

        async def preflight():
            reason = await send_gate(self.risk_guard.db, account.account_id, target, content, media_url,
                                     reservation_token=reservation_token)
            if reason:
                raise TelegramSendPreflightError(reason)
            await validate_live_send(self.risk_guard.db, client, account.account_id, target)

        await preflight()
        audit, group, membership = await current_authorization(self.risk_guard.db, account.account_id, target)
        if not audit or not group or not membership:
            raise TelegramSendPreflightError("qualification_review_required")
        import hashlib
        context = {"content_hash": hashlib.sha256(content.encode()).hexdigest(),
                   "qualification_audit_id": audit.id, "evidence_hash": audit.evidence_hash,
                   "content_scope": audit.content_scope, "policy_version": audit.policy_version,
                   "telegram_group_id": group.group_id, "membership_id": membership.id}
        from app.modules.acquisition.adaptive_frequency import enabled, frequency_context
        if await enabled(self.risk_guard.db, account.account_id):
            from sqlalchemy import select
            from app.modules.acquisition.models import AdDeliveryLog
            reserved = await self.risk_guard.db.scalar(select(AdDeliveryLog).where(
                AdDeliveryLog.reservation_token == reservation_token,
                AdDeliveryLog.account_id == account.account_id,
                AdDeliveryLog.group_id == group.id,
            ))
            if reserved is None or not frequency_context(reserved):
                raise TelegramSendPreflightError("frequency_reservation_required")
            context["frequency"] = frequency_context(reserved)
        async def write():
            if media_url:
                return await client.send_file(group.group_id, media_url, caption=content)
            return await client.send_message(group.group_id, content, link_preview=False)
        result = await self._budgeted_write(account, AccountRiskAction.AD_DELIVERY,
            target=group.group_id, category="ad", attempt_key="ad:" + reservation_token if reservation_token else "",
            context=context, details={"source": source, "content": content, "media_url": media_url,
                "delivery_policy": delivery_policy, "reservation_token": reservation_token},
            write=write, preflight=preflight, on_send_attempted=on_send_attempted)
        return int(getattr(result, "id", None) or result.message_id)

    async def _verification_preflight(self, account: Any) -> None:
        if self.risk_guard is None:
            raise TelegramSendPreflightError("verification_risk_guard_missing")
        from app.modules.acquisition.qualification_service import (
            account_block_reason, policy, verification_account_allowed,
        )
        from app.core.account.models import TelegramAccount
        from datetime import datetime
        config = await policy(self.risk_guard.db, fresh=True)
        if not config.get("enabled") or not config.get("execute_verification"):
            raise TelegramSendPreflightError("qualification_verification_paused")
        if not verification_account_allowed(config, account.account_id):
            raise TelegramSendPreflightError("qualification_account_outside_verification_scope")
        if account_block_reason(await self.risk_guard.db.get(TelegramAccount, account.account_id, populate_existing=True), datetime.utcnow()):
            raise TelegramSendPreflightError("qualification_account_unavailable")

    async def _bot_verification_preflight(self, account: Any) -> None:
        await self._verification_preflight(account)
        from app.core.automation_settings import get_auto_join_scheduler_settings

        settings = await get_auto_join_scheduler_settings(self.risk_guard.db)
        if not settings.get("join_verification", {}).get("allow_second_hop_bots"):
            raise TelegramSendPreflightError("qualification_second_hop_paused")

    async def send_verification_answer(self, account: Any, entity: Any, text: str, *,
            challenge_key: str, reply_to: int | None = None, source: str = "qualification_verification") -> Any:
        import hashlib
        from telethon.utils import get_peer_id
        client = self._get_client(account)
        if client is None or not challenge_key or not text.strip() or reply_to is None:
            raise TelegramSendPreflightError("verification_context_required")
        await self._verification_preflight(account)
        target = get_peer_id(entity)
        async def write():
            return await client.send_message(entity, text, reply_to=reply_to, link_preview=False)
        return await self._budgeted_write(account, AccountRiskAction.VERIFICATION_ANSWER,
            target=target, category="verification", attempt_key="verification:" + hashlib.sha256(challenge_key.encode()).hexdigest(),
            context={"challenge_key": challenge_key, "reply_to": reply_to},
            details={"source": source, "content": text, "challenge_key": challenge_key}, write=write,
            preflight=lambda: self._verification_preflight(account), require_budget=True)

    async def send_verification_bot_text(
        self, account: Any, bot_entity: Any, text: str, *, challenge_key: str,
        group_id: int, source: str = "qualification_second_hop",
    ) -> Any:
        """Persist and budget one reply to a bot named by a verified group challenge."""
        import hashlib
        from telethon.utils import get_peer_id

        client = self._get_client(account)
        if (client is None or getattr(bot_entity, "bot", None) is not True
                or not challenge_key or not text.strip() or len(text) > 160
                or "\n" in text or "\r" in text):
            raise TelegramSendPreflightError("verification_bot_context_required")
        await self._bot_verification_preflight(account)
        target = get_peer_id(bot_entity)

        async def preflight():
            await self._bot_verification_preflight(account)
            current = await client.get_entity(bot_entity)
            if (getattr(current, "bot", None) is not True
                    or get_peer_id(current) != target):
                raise TelegramSendPreflightError("verification_bot_identity_changed")

        async def write():
            return await client.send_message(bot_entity, text, link_preview=False)

        return await self._budgeted_write(
            account, AccountRiskAction.VERIFICATION_ANSWER,
            target=target, category="verification",
            attempt_key="verification_bot:" + hashlib.sha256(challenge_key.encode()).hexdigest(),
            context={"challenge_key": challenge_key, "group_id": group_id,
                     "bot_id": target, "content_hash": hashlib.sha256(text.encode()).hexdigest()},
            details={"source": source, "content": text, "challenge_key": challenge_key},
            write=write, preflight=preflight, require_budget=True,
            risk_target_type="verification_bot",
        )

    async def click_verification_button(self, account: Any, entity: Any, message_id: int, data: bytes, *,
            challenge_key: str, source: str = "qualification_verification",
            risk_target_type: str = "group") -> Any:
        from telethon.errors import RPCError
        from telethon.tl.functions.messages import GetBotCallbackAnswerRequest
        from telethon.utils import get_peer_id
        client = self._get_client(account)
        if client is None or not challenge_key or not isinstance(data, bytes) or message_id <= 0:
            raise TelegramSendPreflightError("verification_callback_context_required")
        preflight = (
            self._bot_verification_preflight
            if risk_target_type == "verification_bot" else self._verification_preflight
        )
        await preflight(account)
        async with self._risk_operation(account, AccountRiskAction.VERIFICATION_CALLBACK,
                target_type=risk_target_type, target_id=get_peer_id(entity),
                details={"source": source, "challenge_key": challenge_key, "message_id": message_id}):
            await preflight(account)
            try:
                result = await client(GetBotCallbackAnswerRequest(peer=entity, msg_id=message_id, data=data))
            except RPCError:
                raise
            except Exception as exc:
                raise TelegramSendOutcomeUnknownError("verification_callback_outcome_unknown") from exc
            if result is None:
                raise TelegramSendOutcomeUnknownError("verification_callback_outcome_unknown")
            return result

    async def check_managed_bot_username(
        self,
        account: Any,
        username: str,
    ) -> bool:
        """Check a managed Bot username through an authenticated user session."""

        client = self._get_client(account)
        if client is None:
            raise TelegramExecutionError("telegram client unavailable")
        normalized_username = _normalize_managed_bot_username(username)
        from telethon.tl.functions.bots import CheckUsernameRequest

        try:
            result = await client(CheckUsernameRequest(username=normalized_username))
        except Exception as exc:
            raise TelegramExecutionError(
                f"managed_bot_username_check_failed:{type(exc).__name__}"
            ) from None
        return bool(result)

    async def create_managed_bot(
        self,
        account: Any,
        *,
        name: str,
        username: str,
        manager_bot: Any,
        source: str = "managed_bot_provision",
        risk_reservation_id: str | None = None,
        on_create_attempted: Callable[[], Awaitable[None]] | None = None,
    ) -> Any:
        """Create one Telegram managed Bot using an authenticated user session."""

        client = self._get_client(account)
        if client is None:
            raise TelegramExecutionError("telegram client unavailable")
        normalized_name = _normalize_managed_bot_name(name)
        normalized_username = _normalize_managed_bot_username(username)
        try:
            manager_entity = await client.get_entity(manager_bot)
        except Exception as exc:
            raise TelegramExecutionError(
                f"managed_bot_manager_resolve_failed:{type(exc).__name__}"
            ) from None
        manager_id = int(getattr(manager_entity, "id", 0) or 0)
        if (
            manager_id <= 0
            or getattr(manager_entity, "bot", None) is not True
            or getattr(manager_entity, "bot_can_manage_bots", None) is not True
        ):
            raise TelegramExecutionError("managed_bot_manager_permission_missing")
        try:
            manager_input = await client.get_input_entity(manager_entity)
        except Exception as exc:
            raise TelegramExecutionError(
                f"managed_bot_manager_input_failed:{type(exc).__name__}"
            ) from None

        if not await self.check_managed_bot_username(account, normalized_username):
            raise TelegramExecutionError("managed_bot_username_unavailable")

        from telethon.tl.functions.bots import CreateBotRequest

        async with self._risk_operation(
            account,
            AccountRiskAction.MANAGED_BOT_CREATE,
            target_type="manager_bot",
            target_id=manager_id,
            details={
                "source": source,
                "risk_reservation_id": risk_reservation_id,
            },
        ):
            if on_create_attempted is not None:
                await on_create_attempted()
            try:
                created = await client(
                    CreateBotRequest(
                        name=normalized_name,
                        username=normalized_username,
                        manager_id=manager_input,
                    )
                )
            except Exception as exc:
                raise TelegramExecutionError(
                    f"managed_bot_create_failed:{type(exc).__name__}"
                ) from None
            created_id = int(getattr(created, "id", 0) or 0)
            created_username = str(getattr(created, "username", "") or "")
            if (
                created_id <= 0
                or getattr(created, "bot", None) is not True
                or created_username.casefold() != normalized_username.casefold()
            ):
                raise TelegramExecutionError("managed_bot_identity_mismatch")
        return created

    async def create_bot_via_botfather(
        self,
        account: Any,
        *,
        name: str,
        username: str,
        source: str = "managed_bot_provision",
        on_username_submitted: Optional[Callable[[], Awaitable[None]]] = None,
        provision_id: Optional[int] = None,
        lease_id: Optional[str] = None,
    ) -> str:
        """Run one leased provision against the verified official BotFather."""
        from datetime import datetime

        from app.core.account.models import ManagedBotProvision

        client = self._get_client(account)
        if client is None:
            raise TelegramExecutionError("telegram client unavailable")
        if (self.risk_guard is None or not getattr(self.risk_guard, "db", None)
                or not provision_id or not lease_id or on_username_submitted is None):
            raise TelegramExecutionError("botfather_provision_context_required")
        normalized_name = _normalize_managed_bot_name(name)
        normalized_username = _normalize_managed_bot_username(username)

        async def active_provision(*, allow_attempted: bool = False) -> Any:
            row = await self.risk_guard.db.get(
                ManagedBotProvision, int(provision_id), populate_existing=True
            )
            if (row is None or row.owner_account_id != getattr(account, "account_id", None)
                    or row.status != "running" or row.current_step != "create_bot"
                    or row.lease_id != lease_id or row.lease_expires_at is None
                    or row.lease_expires_at <= datetime.utcnow()
                    or row.username.casefold() != normalized_username.casefold()
                    or row.display_name != normalized_name
                    or not row.idempotency_key or not re.fullmatch(r"[0-9a-f]{64}", row.request_hash or "")
                    or row.bot_user_id is not None or row.external_created_at is not None
                    or (row.create_attempted_at is not None and not allow_attempted)):
                raise TelegramExecutionError("botfather_provision_lease_invalid")
            return row

        row = await active_provision()
        botfather = await client.get_entity("BotFather")
        if (int(getattr(botfather, "id", 0) or 0) != 93372553
                or getattr(botfather, "bot", None) is not True
                or getattr(botfather, "verified", None) is not True
                or str(getattr(botfather, "username", "") or "").casefold() != "botfather"):
            raise TelegramExecutionError("botfather_official_identity_invalid")

        async def fence_username_submission() -> None:
            await active_provision()
            await on_username_submitted()
            recorded = await active_provision(allow_attempted=True)
            if recorded.create_attempted_at is None:
                raise TelegramExecutionError("botfather_create_fence_missing")

        async with self._risk_operation(
            account,
            AccountRiskAction.MANAGED_BOT_CREATE,
            target_type="official_botfather",
            target_id=93372553,
            details={
                "source": "managed_bot_provision",
                "provision_id": int(row.id),
                "risk_reservation_id": f"managed-bot-{row.id}-{row.request_hash[:16]}",
            },
        ):
            # Revalidate after the asynchronous risk reservation before /newbot.
            await active_provision()
            try:
                return await self._botfather_new_bot_conversation(
                    client,
                    botfather=botfather,
                    name=normalized_name,
                    username=normalized_username,
                    on_username_submitted=fence_username_submission,
                )
            except TelegramExecutionError:
                raise
            except Exception as exc:
                raise TelegramExecutionError(
                    f"botfather_conversation_failed:{type(exc).__name__}"
                ) from None

    async def _botfather_new_bot_conversation(
        self,
        client: Any,
        *,
        botfather: Any,
        name: str,
        username: str,
        on_username_submitted: Optional[Callable[[], Awaitable[None]]],
    ) -> str:
        async with client.conversation(botfather, timeout=45) as conv:
            await conv.send_message("/newbot")
            await self._botfather_expect(conv, ("name", "robot"))
            await conv.send_message(name)
            await self._botfather_expect(conv, ("username",))
            if on_username_submitted is not None:
                await on_username_submitted()
            await conv.send_message(username)
            reply = await conv.get_response()
            token = _extract_botfather_token(reply.message)
            if token:
                return token
            lowered = (reply.message or "").lower()
            if "taken" in lowered or "unavailable" in lowered or "in use" in lowered:
                raise TelegramExecutionError("botfather_username_taken")
            raise TelegramExecutionError(
                f"botfather_create_rejected:{(reply.message or '')[:120]}"
            )

    async def _botfather_expect(self, conv: Any, keywords: tuple[str, ...]) -> Any:
        reply = await conv.get_response()
        text = str(getattr(reply, "message", "") or "").lower()
        if not any(keyword in text for keyword in keywords):
            raise TelegramExecutionError(f"botfather_flow_unexpected:{text[:120]}")
        return reply

    async def read_botfather_provisioned_token(
        self,
        account: Any,
        *,
        username: str,
        source: str = "managed_bot_provision",
    ) -> Optional[str]:
        """Recover a provisioned bot's token from the @BotFather chat history.

        Used when a previous conversation submitted the username but died
        before the token could be parsed.  Returns ``None`` when no token
        message for this username is found.
        """

        client = self._get_client(account)
        if client is None:
            raise TelegramExecutionError("telegram client unavailable")
        normalized_username = _normalize_managed_bot_username(username)
        async with self._risk_operation(
            account,
            AccountRiskAction.PRIVATE_MESSAGE,
            target_type="user",
            target_id="botfather",
            details={"source": source},
        ):
            messages = await client.get_messages("botfather", limit=50)
        for message in messages or []:
            text = str(getattr(message, "message", "") or "")
            if normalized_username.casefold() not in text.casefold():
                continue
            token = _extract_botfather_token(text)
            if token:
                return token
        return None

    async def check_spambot_status(
        self,
        account: Any,
        *,
        wait_seconds: int = 20,
        source: str = "account_spam_check",
        attempt_key: str | None = None,
    ) -> str:
        """Ask Telegram's official @SpamBot and return its reply for classification."""

        client = self._get_client(account)
        if client is None:
            raise TelegramExecutionError("telegram client unavailable")
        try:
            entity = await client.get_entity("SpamBot")
        except Exception as exc:
            raise TelegramExecutionError(f"spambot_resolve_failed:{type(exc).__name__}") from exc
        username = str(getattr(entity, "username", "") or "").casefold()
        entity_id = getattr(entity, "id", None)
        if (
            username != "spambot"
            or getattr(entity, "bot", None) is not True
            or entity_id is None
            or int(entity_id) != _OFFICIAL_SPAMBOT_ID
        ):
            raise TelegramExecutionError("official_spambot_identity_mismatch")

        async def write():
            return await client.send_message(entity, "/start")
        sent = None
        budget = await self._outbound_service(account)
        if budget and attempt_key:
            from sqlalchemy import select
            from app.core.account.models import AccountOutboundAttempt
            original = await budget.db.scalar(select(AccountOutboundAttempt).where(
                AccountOutboundAttempt.account_id == account.account_id,
                AccountOutboundAttempt.attempt_key == attempt_key))
            if original is not None and original.attempted_at is not None:
                if not original.message_id:
                    raise TelegramSendOutcomeUnknownError("spambot_original_request_requires_reconciliation")
                sent = await client.get_messages(entity, ids=original.message_id)
                if isinstance(sent, list):
                    sent = sent[0] if sent else None
                if sent is None or not getattr(sent, "out", False):
                    raise TelegramSendOutcomeUnknownError("spambot_original_request_unavailable")
        if sent is None:
            sent = await self._budgeted_write(account, AccountRiskAction.SPAM_CHECK,
                target=int(entity_id), category="diagnostic", attempt_key=attempt_key or "",
                context={"source": "account_spam_check"}, details={"source": source}, write=write)
        try:
            sent_id = int(getattr(sent, "id", 0) or 0)
            sent_at = getattr(sent, "date", None)
            if sent_id <= 0 or sent_at is None:
                raise TelegramExecutionError("spambot_request_identity_missing")
            deadline = asyncio.get_running_loop().time() + max(2, min(int(wait_seconds), 60))
            while True:
                messages = await client.get_messages(entity, limit=10)
                if messages is None:
                    candidates = []
                elif isinstance(messages, (list, tuple)):
                    candidates = list(messages)
                else:
                    try:
                        candidates = list(messages)
                    except TypeError:
                        candidates = [messages]
                for message in candidates:
                    message_id = int(getattr(message, "id", 0) or 0)
                    sender_id = getattr(message, "sender_id", None)
                    if sender_id is None:
                        sender_id = getattr(
                            getattr(message, "from_id", None),
                            "user_id",
                            None,
                        )
                    message_at = getattr(message, "date", None)
                    if (
                        message_id <= sent_id
                        or message_at is None
                        or message_at < sent_at
                        or bool(getattr(message, "out", False))
                        or sender_id is None
                        or int(sender_id) != _OFFICIAL_SPAMBOT_ID
                    ):
                        continue
                    text = str(
                        getattr(message, "raw_text", None)
                        or getattr(message, "message", None)
                        or ""
                    ).strip()
                    if text:
                        return text
                if asyncio.get_running_loop().time() >= deadline:
                    raise TelegramExecutionError("spambot_response_timeout")
                await asyncio.sleep(2)
        except TelegramExecutionError:
            raise
        except Exception as exc:
            raise TelegramExecutionError(
                f"spambot_request_failed:{type(exc).__name__}"
            ) from exc

    async def update_profile_bio(
        self,
        account: Any,
        bio: str,
        *,
        source: str = "account_profile",
    ) -> bool:
        client = self._get_client(account)
        if client is None:
            return False

        normalized_bio = (bio or "").strip()[:70]
        async with self._risk_operation(
            account,
            AccountRiskAction.PROFILE_UPDATE,
            target_type="account",
            target_id=getattr(account, "account_id", None),
            details={"source": source, "bio_length": len(normalized_bio)},
        ):
            from telethon import functions

            await client(functions.account.UpdateProfileRequest(about=normalized_bio))
        return True

    async def create_channel(
        self,
        account: Any,
        title: str,
        *,
        about: str = "",
        source: str = "managed_channel_create",
    ) -> Any:
        """Create a Telegram broadcast channel with an authenticated user account."""
        client = self._get_client(account)
        if client is None:
            raise TelegramExecutionError("telegram client unavailable")

        from telethon import functions

        async with self._risk_operation(
            account,
            AccountRiskAction.CHANNEL_CREATE,
            target_type="account",
            target_id=getattr(account, "account_id", None),
            details={"source": source, "title_length": len(title), "about_length": len(about)},
        ):
            result = await client(
                functions.channels.CreateChannelRequest(
                    title=title,
                    about=about,
                    broadcast=True,
                    megagroup=False,
                )
            )
        channels = list(getattr(result, "chats", None) or [])
        if not channels:
            raise TelegramExecutionError("Telegram did not return the created channel")
        return channels[0]

    async def update_channel_username(
        self,
        account: Any,
        channel_id: int | str,
        username: str,
        *,
        source: str = "managed_channel_username",
    ) -> bool:
        """Set or remove the public username of a channel owned by a user account."""
        client = self._get_client(account)
        if client is None:
            raise TelegramExecutionError("telegram client unavailable")

        from telethon import functions

        async with self._risk_operation(
            account,
            AccountRiskAction.PROFILE_UPDATE,
            target_type="channel",
            target_id=channel_id,
            details={"source": source, "username": username or None},
        ):
            entity = await client.get_entity(channel_id)
            result = await client(
                functions.channels.UpdateUsernameRequest(channel=entity, username=username)
            )
        return bool(result)

    async def set_default_chat_permissions(
        self,
        client: Any,
        chat_id: int | str,
        permissions: dict[str, bool],
        *,
        source: str = "managed_group_permissions",
    ) -> bool:
        """Set group-wide default member permissions through the Bot API."""
        async with self._risk_operation(
            self._bot_account,
            AccountRiskAction.MODERATION,
            target_type="group",
            target_id=chat_id,
            details={"source": source, "permissions": permissions},
        ):
            return bool(
                await client.set_chat_permissions(
                    chat_id,
                    permissions,
                    use_independent_chat_permissions=True,
                )
            )

    async def message_exists(self, account: Any, target: int | str, message_id: int) -> bool:
        client = self._get_client(account)
        if client is None:
            raise TelegramExecutionError("telegram client unavailable")

        if not hasattr(client, "get_messages"):
            raise TelegramExecutionError("telegram client does not support get_messages")

        try:
            result = await client.get_messages(target, ids=int(message_id))
        except TypeError:
            result = await client.get_messages(target, message_ids=int(message_id))

        if isinstance(result, list):
            return any(item is not None and not getattr(item, "empty", False) for item in result)
        return result is not None and not getattr(result, "empty", False)

    async def send_bot_message(
        self,
        client: Any,
        chat_id: int | str,
        message: str,
        *,
        parse_mode: Optional[str] = "Markdown",
        disable_web_page_preview: bool = False,
        disable_notification: bool = False,
        reply_markup: Optional[dict[str, Any]] = None,
        source: str = "bot_api",
    ) -> Optional[int]:
        async with self._risk_operation(
            self._bot_account,
            AccountRiskAction.BOT_MESSAGE,
            target_type="chat",
            target_id=chat_id,
            details={"source": source, "content": message},
        ):
            result = await client.send_message(
                chat_id,
                message,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
                disable_notification=disable_notification,
                reply_markup=reply_markup,
            )
        return getattr(result, "message_id", getattr(result, "id", None))

    async def send_pinned_bot_message(
        self,
        client: Any,
        chat_id: int | str,
        message: str,
        *,
        parse_mode: Optional[str] = "Markdown",
        disable_web_page_preview: bool = False,
        disable_notification: bool = True,
        reply_markup: Optional[dict[str, Any]] = None,
        source: str = "bot_api",
    ) -> Optional[int]:
        message_id = await self.send_bot_message(
            client,
            chat_id,
            message,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
            disable_notification=disable_notification,
            reply_markup=reply_markup,
            source=source,
        )
        if message_id is None:
            return None

        async with self._risk_operation(
            self._bot_account,
            AccountRiskAction.BOT_PIN,
            target_type="message",
            target_id=message_id,
            details={"source": source, "chat_id": chat_id},
        ):
            await client.pin_chat_message(
                chat_id,
                message_id,
                disable_notification=disable_notification,
            )
        return message_id

    async def delete_message(
        self,
        account: Any,
        chat_id: int | str,
        message_id: int,
        *,
        source: str = "unknown",
    ) -> bool:
        client = self._get_client(account)
        if client is None:
            return False

        async with self._risk_operation(
            account,
            AccountRiskAction.MODERATION,
            target_type="message",
            target_id=message_id,
            details={"source": source, "chat_id": chat_id},
        ):
            if hasattr(client, "delete_message"):
                await client.delete_message(chat_id, message_id)
            elif hasattr(client, "delete_messages"):
                await client.delete_messages(chat_id, [message_id], revoke=True)
            else:
                raise TelegramExecutionError("telegram client does not support message deletion")
        return True

    async def mute_user(
        self,
        account: Any,
        chat_id: int,
        user_id: int,
        duration: int,
        *,
        source: str = "unknown",
    ) -> bool:
        client = self._get_client(account)
        if client is None:
            return False

        until_date = duration if duration > 0 else 30 * 60
        async with self._risk_operation(
            account,
            AccountRiskAction.MODERATION,
            target_type="user",
            target_id=user_id,
            details={"source": source, "chat_id": chat_id, "duration": duration},
        ):
            await client.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                until_date=until_date,
                can_send_messages=False,
                can_send_media_messages=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False,
            )
        return True

    async def unmute_user(
        self,
        account: Any,
        chat_id: int,
        user_id: int,
        *,
        source: str = "unknown",
    ) -> bool:
        client = self._get_client(account)
        if client is None:
            return False

        async with self._risk_operation(
            account,
            AccountRiskAction.MODERATION,
            target_type="user",
            target_id=user_id,
            details={"source": source, "chat_id": chat_id, "operation": "unmute"},
        ):
            await client.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            )
        return True

    async def ban_user(
        self,
        account: Any,
        chat_id: int,
        user_id: int,
        *,
        source: str = "unknown",
    ) -> bool:
        client = self._get_client(account)
        if client is None:
            return False

        async with self._risk_operation(
            account,
            AccountRiskAction.MODERATION,
            target_type="user",
            target_id=user_id,
            details={"source": source, "chat_id": chat_id, "operation": "ban"},
        ):
            await client.ban_chat_member(chat_id, user_id)
        return True

    async def unban_user(
        self,
        account: Any,
        chat_id: int,
        user_id: int,
        *,
        source: str = "unknown",
    ) -> bool:
        client = self._get_client(account)
        if client is None:
            return False

        async with self._risk_operation(
            account,
            AccountRiskAction.MODERATION,
            target_type="user",
            target_id=user_id,
            details={"source": source, "chat_id": chat_id, "operation": "unban"},
        ):
            await client.unban_chat_member(chat_id, user_id)
        return True

    async def send_reaction(
        self,
        account: Any,
        group_id: int,
        message_id: int,
        emoji: str,
        *,
        source: str = "unknown",
    ) -> bool:
        client = self._get_client(account)
        if client is None:
            return False

        async with self._risk_operation(
            account,
            AccountRiskAction.REACTION,
            target_type="message",
            target_id=message_id,
            details={"source": source, "group_id": group_id, "emoji": emoji},
        ):
            if hasattr(client, "send_reaction"):
                await client.send_reaction(group_id, message_id, emoji)
            elif hasattr(client, "send_reaction_request"):
                await client.send_reaction_request(group_id, message_id, emoji)
            else:
                return False
        return True

    async def pin_message(
        self,
        account: Any,
        group_id: int,
        message_id: int,
        *,
        source: str = "unknown",
    ) -> bool:
        client = self._get_client(account)
        if client is None or not hasattr(client, "pin_message"):
            return False

        async with self._risk_operation(
            account,
            AccountRiskAction.PIN,
            target_type="message",
            target_id=message_id,
            details={"source": source, "group_id": group_id},
        ):
            await client.pin_message(group_id, message_id)
        return True

    async def forward_message(
        self,
        account: Any,
        from_chat_id: int,
        to_chat_id: int,
        message_id: int,
        *,
        source: str = "unknown",
    ) -> Optional[int]:
        client = self._get_client(account)
        if client is None:
            return None

        async with self._risk_operation(
            account,
            AccountRiskAction.FORWARD,
            target_type="chat",
            target_id=to_chat_id,
            details={"source": source, "from_chat_id": from_chat_id, "message_id": message_id},
        ):
            result = await client.forward_messages(to_chat_id, message_id, from_chat_id)
        return getattr(result, "id", getattr(result, "message_id", None))

    async def _validate_qualification_join(
        self, entity: Any, *, action: AccountRiskAction = AccountRiskAction.JOIN
    ) -> None:
        # Owned-group resource workflows have their own operation/owner protections.
        # No caller-controlled source string can bypass a normal JOIN action.
        if action == AccountRiskAction.OWNED_GROUP_JOIN or self.risk_guard is None:
            return
        db = getattr(self.risk_guard, "db", None)
        if db is None:
            return
        from app.modules.acquisition.qualification_join_gate import qualification_join_gate

        try:
            reason = await qualification_join_gate(db, entity)
        except Exception as exc:
            raise TelegramExecutionError("qualification_join_check_unavailable") from exc
        if reason:
            raise TelegramExecutionError(reason)

    async def join_group(
        self,
        account: Any,
        group: Any,
        *,
        source: str = "auto_join",
        join_reservation_key: str | None = None,
        on_join_request_attempted: Optional[
            Callable[[], Awaitable[None] | None]
        ] = None,
    ) -> None:
        client = self._get_client(account)
        if client is None:
            raise TelegramExecutionError("telegram client unavailable")

        target = getattr(group, "username", None)
        if not target:
            raise TelegramExecutionError("public username is required for auto join")
        target = target.lstrip("@")

        async with self._risk_operation(
            account,
            AccountRiskAction.JOIN,
            target_type="group",
            target_id=target or getattr(group, "group_id", None),
            details={"source": source, "join_reservation_key": join_reservation_key},
        ):
            entity = await client.get_entity(target)
            if not _is_joinable_telegram_entity(entity):
                raise TelegramExecutionError(
                    "target is a channel, only groups are allowed for auto join"
                )

            stored_group_id = getattr(group, "group_id", None)
            if stored_group_id is not None:
                from app.modules.acquisition.qualification_identity import (
                    entity_identity,
                    peer_identity,
                )

                stored_identity = peer_identity(stored_group_id)
                resolved_identity = entity_identity(entity)
                if (
                    stored_identity is None
                    or resolved_identity is None
                    or stored_identity[0] != resolved_identity[0]
                    or (
                        stored_identity[1] is not None
                        and stored_identity[1] != resolved_identity[1]
                    )
                ):
                    raise TelegramExecutionError("join_target_identity_changed")

            from telethon.tl.functions.channels import JoinChannelRequest

            await self._validate_qualification_join(entity)
            await self._notify_join_request_attempted(on_join_request_attempted)
            await client(JoinChannelRequest(entity))

    async def join_group_by_link(
        self,
        account: Any,
        group_link: str,
        *,
        source: str = "manual_link_join",
        join_reservation_key: str | None = None,
        action: AccountRiskAction = AccountRiskAction.JOIN,
        on_join_request_attempted: Optional[
            Callable[[], Awaitable[None] | None]
        ] = None,
    ) -> dict[str, Any]:
        """Join a public group or private invite and return resolved chat data."""
        client = self._get_client(account)
        if client is None:
            raise TelegramExecutionError("telegram client unavailable")

        parsed = parse_telegram_group_link(group_link)
        risk_target = parsed.target if parsed.kind == "public" else "private_invite"
        if parsed.kind == "public":
            entity = await client.get_entity(parsed.target)
            if not _is_joinable_telegram_entity(entity):
                raise TelegramExecutionError(
                    "target is a broadcast channel; only groups and supergroups are allowed"
                )
            from telethon.tl.functions.channels import JoinChannelRequest

            async with self._risk_operation(
                account,
                action,
                target_type="group",
                target_id=risk_target,
                details={"source": source, "link_type": parsed.kind, "join_reservation_key": join_reservation_key},
            ):
                await self._validate_qualification_join(entity, action=action)
                await self._notify_join_request_attempted(on_join_request_attempted)
                try:
                    await client(JoinChannelRequest(entity))
                except Exception as exc:
                    if exc.__class__.__name__ != "UserAlreadyParticipantError":
                        raise
        else:
            from telethon.tl.functions.messages import CheckChatInviteRequest
            from telethon.tl.types import ChatInviteAlready

            preview = await client(CheckChatInviteRequest(parsed.target))
            existing_chat = getattr(preview, "chat", None)
            if existing_chat is not None and not _is_joinable_telegram_entity(existing_chat):
                raise TelegramExecutionError(
                    "target is a broadcast channel; only groups and supergroups are allowed"
                )
            if isinstance(preview, ChatInviteAlready):
                entity = existing_chat
            else:
                if getattr(preview, "broadcast", False) and not (
                    getattr(preview, "megagroup", False)
                    or getattr(preview, "gigagroup", False)
                ):
                    raise TelegramExecutionError(
                        "target is a broadcast channel; only groups and supergroups are allowed"
                    )
                async with self._risk_operation(
                    account,
                    action,
                    target_type="group",
                    target_id=risk_target,
                    details={"source": source, "link_type": parsed.kind, "join_reservation_key": join_reservation_key},
                ):
                    await self._validate_qualification_join(existing_chat, action=action)
                    await self._notify_join_request_attempted(
                        on_join_request_attempted
                    )
                    entity = await self._join_private_group(
                        client, parsed.target, preview=preview
                    )

        if not _is_joinable_telegram_entity(entity):
            raise TelegramExecutionError(
                "target is a broadcast channel; only groups and supergroups are allowed"
            )

        from telethon import utils

        from app.modules.acquisition.search.group_finder import telegram_chat_to_dict

        data = telegram_chat_to_dict(entity)
        raw_id = int(data.get("id") or 0)
        try:
            data["id"] = int(utils.get_peer_id(entity))
        except (TypeError, ValueError):
            data["id"] = raw_id
        data["raw_id"] = raw_id
        if not data["id"]:
            raise TelegramExecutionError("Telegram did not return the joined group ID")
        return data

    async def resolve_join_group_by_link_membership(
        self,
        account: Any,
        group_link: str | int,
    ) -> dict[str, Any] | None:
        """Resolve an already-approved join request without sending another join."""

        client = self._get_client(account)
        if client is None:
            raise TelegramExecutionError("telegram client unavailable")

        parsed = parse_telegram_group_link(group_link) if isinstance(group_link, str) else None
        confirmed_by_invite = False
        if parsed is not None and parsed.kind == "private":
            from telethon.tl.functions.messages import CheckChatInviteRequest
            from telethon.tl.types import ChatInviteAlready

            preview = await client(CheckChatInviteRequest(parsed.target))
            entity = getattr(preview, "chat", None)
            if entity is None:
                return None
            confirmed_by_invite = isinstance(preview, ChatInviteAlready)
        else:
            entity = await client.get_entity(parsed.target if parsed else group_link)

        if not _is_joinable_telegram_entity(entity):
            raise TelegramExecutionError(
                "target is a broadcast channel; only groups and supergroups are allowed"
            )

        if not confirmed_by_invite:
            try:
                if hasattr(client, "get_permissions"):
                    permission = await client.get_permissions(entity, "me")
                    if permission is None or not hasattr(permission, "has_left"):
                        raise TelegramExecutionError("join_membership_permission_unknown")
                    participant = getattr(permission, "participant", None)
                    if (permission.has_left or getattr(entity, "left", False)
                            or type(participant).__name__.endswith("Left")
                            or getattr(getattr(participant, "banned_rights", None), "view_messages", False)):
                        return None
                else:
                    from telethon.tl.functions.channels import GetParticipantRequest

                    me = await client.get_me()
                    await client(GetParticipantRequest(entity, me))
            except Exception as exc:
                if exc.__class__.__name__ in {
                    "UserNotParticipantError",
                }:
                    return None
                raise

        from telethon import utils

        from app.modules.acquisition.search.group_finder import telegram_chat_to_dict

        data = telegram_chat_to_dict(entity)
        raw_id = int(data.get("id") or 0)
        try:
            data["id"] = int(utils.get_peer_id(entity))
        except (TypeError, ValueError):
            data["id"] = raw_id
        data["raw_id"] = raw_id
        if not data["id"]:
            raise TelegramExecutionError("Telegram did not return the joined group ID")
        return data

    async def _join_private_group(
        self,
        client: Any,
        invite_hash: str,
        *,
        preview: Any | None = None,
    ) -> Any:
        from telethon.tl.functions.messages import (
            CheckChatInviteRequest,
            ImportChatInviteRequest,
        )
        from telethon.tl.types import ChatInviteAlready

        preview = preview or await client(CheckChatInviteRequest(invite_hash))
        existing_chat = getattr(preview, "chat", None)
        if isinstance(preview, ChatInviteAlready):
            return existing_chat
        if getattr(preview, "broadcast", False) and not (
            getattr(preview, "megagroup", False) or getattr(preview, "gigagroup", False)
        ):
            raise TelegramExecutionError(
                "target is a broadcast channel; only groups and supergroups are allowed"
            )

        try:
            result = await client(ImportChatInviteRequest(invite_hash))
        except Exception as exc:
            error_name = exc.__class__.__name__
            if error_name == "InviteRequestSentError":
                raise TelegramJoinRequestPendingError(
                    "Telegram join request is awaiting group approval"
                ) from exc
            if error_name != "UserAlreadyParticipantError":
                raise
            checked = await client(CheckChatInviteRequest(invite_hash))
            existing_chat = getattr(checked, "chat", None)
            if not isinstance(checked, ChatInviteAlready):
                raise TelegramExecutionError("unable to resolve the group already joined") from exc
            return existing_chat

        chats = list(getattr(result, "chats", None) or [])
        for chat in chats:
            if _is_joinable_telegram_entity(chat):
                return chat
        raise TelegramExecutionError("Telegram did not return the joined group")

    async def leave_group(
        self,
        account: Any,
        entity: Any,
        *,
        group_id: int,
        source: str = "auto_join",
    ) -> None:
        if self.risk_guard is not None and await is_owned_group_target(
            self.risk_guard.db, telegram_group_id=group_id,
        ):
            raise TelegramExecutionError("owned_group_protected")
        client = self._get_client(account)
        if client is None:
            raise TelegramExecutionError("telegram client unavailable")

        from telethon.tl.functions.channels import LeaveChannelRequest
        from telethon.tl.functions.messages import DeleteChatUserRequest

        try:
            await client(LeaveChannelRequest(entity))
            return
        except Exception:
            if (
                getattr(entity, "megagroup", False)
                or getattr(entity, "gigagroup", False)
                or getattr(entity, "broadcast", False)
            ):
                raise

        user = "me"
        if hasattr(client, "get_me"):
            user = await client.get_me()
        chat_id = getattr(entity, "id", group_id)
        await client(DeleteChatUserRequest(chat_id, user))

    async def leave_group_by_id(
        self,
        account: Any,
        group_id: int,
        *,
        source: str = "group_write_forbidden",
    ) -> None:
        """Resolve a known group ID and leave it."""
        client = self._get_client(account)
        if client is None:
            raise TelegramExecutionError("telegram client unavailable")
        entity = await client.get_input_entity(group_id)
        await self.leave_group(account, entity, group_id=group_id, source=source)
