"""Telegram execution helpers.

This module keeps Telegram client calls behind a narrow execution boundary so
business workflows can ask for an action without reimplementing the send/join
plumbing every time.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from app.core.account.risk_guard import AccountRiskAction, AccountRiskGuard
from app.core.account.system_identity import bot_risk_identity


class TelegramExecutionError(RuntimeError):
    """Raised when a Telegram operation cannot be executed."""


class TelegramJoinRequestPendingError(TelegramExecutionError):
    """Raised when Telegram accepted a join request that still needs approval."""


@dataclass(frozen=True)
class ParsedTelegramGroupLink:
    """Validated Telegram group link without any unrelated URL components."""

    kind: str
    target: str


_TELEGRAM_LINK_HOSTS = {"t.me", "telegram.me"}
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
    return 4 <= len(value) <= 32 and value[0].isalpha() and all(
        char.isascii() and (char.isalnum() or char == "_") for char in value
    )


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

        decision = await self.risk_guard.check_and_reserve(
            account,
            action,
            target_type=target_type,
            target_id=target_id,
            details=details,
        )
        if not decision.allowed:
            raise TelegramExecutionError(f"risk_guard_blocked:{decision.reason}")

        try:
            yield
        except Exception as exc:
            await self.risk_guard.record_failure(
                account,
                action,
                exc,
                target_type=target_type,
                target_id=target_id,
                details=details,
            )
            raise
        else:
            await self.risk_guard.record_success(
                account,
                action,
                target_type=target_type,
                target_id=target_id,
                details=details,
            )

    async def send_private_message(
        self,
        account: Any,
        user_id: int,
        message: str,
        *,
        initiated_by_user: bool = False,
        source: str = "unknown",
    ) -> bool:
        client = self._get_client(account)
        if client is None:
            return False

        async with self._risk_operation(
            account,
            AccountRiskAction.PRIVATE_MESSAGE,
            target_type="user",
            target_id=user_id,
            details={"source": source, "initiated_by_user": initiated_by_user, "content": message},
        ):
            await client.send_message(user_id, message)
        return True

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
        return getattr(result, "id", getattr(result, "message_id", None))

    async def send_ad(
        self,
        account: Any,
        target: int | str,
        content: str,
        *,
        media_url: Optional[str] = None,
        source: str = "acquisition_ad",
    ) -> Optional[int]:
        client = self._get_client(account)
        if client is None:
            return None

        async with self._risk_operation(
            account,
            AccountRiskAction.AD_DELIVERY,
            target_type="group",
            target_id=target,
            details={"source": source, "content": content, "media_url": media_url},
        ):
            if media_url:
                result = await client.send_file(target, media_url, caption=content)
            else:
                result = await client.send_message(target, content)
        return getattr(result, "id", getattr(result, "message_id", None))

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

    async def join_group(
        self,
        account: Any,
        group: Any,
        *,
        source: str = "auto_join",
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
            details={"source": source},
        ):
            entity = await client.get_entity(target)
            if not _is_joinable_telegram_entity(entity):
                raise TelegramExecutionError(
                    "target is a channel, only groups are allowed for auto join"
                )

            from telethon.tl.functions.channels import JoinChannelRequest

            await client(JoinChannelRequest(entity))

    async def join_group_by_link(
        self,
        account: Any,
        group_link: str,
        *,
        source: str = "manual_link_join",
    ) -> dict[str, Any]:
        """Join a public group or private invite and return resolved chat data."""
        client = self._get_client(account)
        if client is None:
            raise TelegramExecutionError("telegram client unavailable")

        parsed = parse_telegram_group_link(group_link)
        risk_target = parsed.target if parsed.kind == "public" else "private_invite"
        async with self._risk_operation(
            account,
            AccountRiskAction.JOIN,
            target_type="group",
            target_id=risk_target,
            details={"source": source, "link_type": parsed.kind},
        ):
            if parsed.kind == "public":
                entity = await client.get_entity(parsed.target)
                if not _is_joinable_telegram_entity(entity):
                    raise TelegramExecutionError(
                        "target is a broadcast channel; only groups and supergroups are allowed"
                    )

                from telethon.tl.functions.channels import JoinChannelRequest

                try:
                    await client(JoinChannelRequest(entity))
                except Exception as exc:
                    if exc.__class__.__name__ != "UserAlreadyParticipantError":
                        raise
            else:
                entity = await self._join_private_group(client, parsed.target)

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

    async def _join_private_group(self, client: Any, invite_hash: str) -> Any:
        from telethon.tl.functions.messages import (
            CheckChatInviteRequest,
            ImportChatInviteRequest,
        )

        preview = await client(CheckChatInviteRequest(invite_hash))
        existing_chat = getattr(preview, "chat", None)
        if existing_chat is not None:
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
            if existing_chat is None:
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
