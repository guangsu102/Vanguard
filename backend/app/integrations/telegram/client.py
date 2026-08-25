"""
Telegram API Client Module

Provides unified interface for Telegram Bot API and User API operations.
Supports both Bot Token and User Session authentication.
"""

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

import httpx
import structlog

from app.core.account.risk_guard import AccountRiskAction, AccountRiskGuard
from app.core.account.system_identity import bot_risk_identity

logger = structlog.get_logger()


class TelegramAPIError(Exception):
    """Telegram API error."""

    def __init__(self, message: str, code: int = None, method: str = None):
        self.message = message
        self.code = code
        self.method = method
        super().__init__(self.message)


class RateLimitError(TelegramAPIError):
    """Rate limit exceeded."""

    pass


@dataclass
class TelegramConfig:
    """Telegram API configuration."""

    bot_token: Optional[str] = None
    api_id: Optional[int] = None
    api_hash: Optional[str] = None
    session_name: str = "vanguard"
    proxy: Optional[str] = None
    timeout: int = 30


@dataclass
class User:
    """Telegram user information."""

    user_id: int
    username: Optional[str] = None
    first_name: str = ""
    last_name: Optional[str] = None
    is_bot: bool = False
    language_code: Optional[str] = None

    @property
    def full_name(self) -> str:
        """Get user's full name."""
        if self.last_name:
            return f"{self.first_name} {self.last_name}"
        return self.first_name

    @property
    def mention(self) -> str:
        """Get user's mention string."""
        if self.username:
            return f"@{self.username}"
        return f"[{self.full_name}](tg://user?id={self.user_id})"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "User":
        """Create User from API response dict."""
        return cls(
            user_id=data.get("id", 0),
            username=data.get("username"),
            first_name=data.get("first_name", ""),
            last_name=data.get("last_name"),
            is_bot=data.get("is_bot", False),
            language_code=data.get("language_code"),
        )


@dataclass
class Chat:
    """Telegram chat (group/channel) information."""

    chat_id: int
    title: Optional[str] = None
    username: Optional[str] = None
    type: str = "private"  # private, group, supergroup, channel
    member_count: Optional[int] = None
    description: Optional[str] = None
    username: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Chat":
        """Create Chat from API response dict."""
        return cls(
            chat_id=data.get("id", 0),
            title=data.get("title"),
            username=data.get("username"),
            type=data.get("type", "private"),
            member_count=data.get("members_count"),
            description=data.get("description"),
        )


@dataclass
class Message:
    """Telegram message."""

    message_id: int
    chat: Chat
    from_user: Optional[User] = None
    text: Optional[str] = None
    date: Optional[int] = None
    reply_to_message_id: Optional[int] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Message":
        """Create Message from API response dict."""
        chat_data = data.get("chat", {})
        from_data = data.get("from")

        return cls(
            message_id=data.get("message_id", 0),
            chat=Chat.from_dict(chat_data),
            from_user=User.from_dict(from_data) if from_data else None,
            text=data.get("text"),
            date=data.get("date"),
            reply_to_message_id=data.get("reply_to_message_id"),
        )


@dataclass
class SearchResult:
    """Telegram search result."""

    chat: Chat
    message_snippet: Optional[str] = None
    date: Optional[int] = None


class TelegramClient:
    """
    Telegram API Client.

    Provides methods for interacting with Telegram Bot API.
    Supports rate limiting, retry logic, and error handling.
    """

    BOT_API_URL = "https://api.telegram.org/bot{token}/{method}"

    # Rate limits
    MESSAGE_RATE_LIMIT = 30  # messages per second
    GROUP_LIMIT = 20  # groups per minute for new members

    def __init__(
        self, config: TelegramConfig, risk_guard: AccountRiskGuard | None = None, risk_account=None
    ):
        """
        Initialize Telegram client.

        Args:
            config: Telegram configuration
        """
        self.config = config
        self._client: Optional[httpx.AsyncClient] = None
        self._rate_limiter = RateLimiter()
        self.risk_guard = risk_guard
        self.risk_account = risk_account or bot_risk_identity("bot_api")
        self.logger = logger.bind(module="telegram_client")

    @asynccontextmanager
    async def _risk_operation(
        self,
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
            self.risk_account,
            action,
            target_type=target_type,
            target_id=target_id,
            details=details,
        )
        if not decision.allowed:
            raise RateLimitError(f"risk_guard_blocked:{decision.reason}")
        try:
            yield
        except Exception as exc:
            await self.risk_guard.record_failure(
                self.risk_account,
                action,
                exc,
                target_type=target_type,
                target_id=target_id,
                details=details,
            )
            raise
        else:
            await self.risk_guard.record_success(
                self.risk_account,
                action,
                target_type=target_type,
                target_id=target_id,
                details=details,
            )

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.config.timeout,
                follow_redirects=True,
            )
        return self._client

    async def close(self):
        """Close HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        retry: int = 3,
        retry_delay: float = 1.0,
    ) -> Dict[str, Any]:
        """
        Make API request with retry logic.

        Args:
            method: API method name
            params: Method parameters
            retry: Number of retries
            retry_delay: Delay between retries

        Returns:
            API response data

        Raises:
            TelegramAPIError: On API error
            RateLimitError: On rate limit
        """
        if not self.config.bot_token:
            raise TelegramAPIError("Bot token not configured", method=method)

        url = self.BOT_API_URL.format(token=self.config.bot_token, method=method)
        client = await self._get_client()

        for attempt in range(retry + 1):
            try:
                self.logger.debug("telegram_api_request", method=method, params=params)

                response = await client.post(url, json=params or {})

                if response.status_code == 429:
                    # Rate limited
                    retry_after = int(response.headers.get("Retry-After", 60))
                    self.logger.warning("rate_limited", method=method, retry_after=retry_after)
                    await asyncio.sleep(retry_after)
                    continue

                data = response.json()

                if not data.get("ok"):
                    error_code = data.get("error_code")
                    description = data.get("description", "Unknown error")

                    if error_code == 429:
                        retry_after = int(data.get("parameters", {}).get("retry_after", 60))
                        self.logger.warning("rate_limited", method=method, retry_after=retry_after)
                        await asyncio.sleep(retry_after)
                        continue

                    raise TelegramAPIError(
                        description,
                        code=error_code,
                        method=method,
                    )

                return data.get("result", {})

            except httpx.HTTPStatusError as e:
                if attempt < retry:
                    await asyncio.sleep(retry_delay * (attempt + 1))
                    retry_delay *= 2
                    continue
                raise TelegramAPIError(str(e), code=e.response.status_code, method=method)

            except httpx.RequestError as e:
                if attempt < retry:
                    await asyncio.sleep(retry_delay * (attempt + 1))
                    retry_delay *= 2
                    continue
                raise TelegramAPIError(str(e), method=method)

        raise TelegramAPIError("Max retries exceeded", method=method)

    # =============================================================================
    # Bot Info
    # =============================================================================

    async def get_me(self) -> User:
        """
        Get bot information.

        Returns:
            Bot user information
        """
        data = await self._request("getMe")
        return User.from_dict(data)

    async def get_updates(
        self,
        offset: int = None,
        limit: int = 100,
        timeout: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        Get updates.

        Args:
            offset: Update ID offset
            limit: Limit of updates
            timeout: Timeout in seconds

        Returns:
            List of updates
        """
        params = {"limit": limit, "timeout": timeout}
        if offset:
            params["offset"] = offset

        data = await self._request("getUpdates", params)
        return data if isinstance(data, list) else []

    # =============================================================================
    # Chat Operations
    # =============================================================================

    async def get_chat(self, chat_id: Union[int, str]) -> Chat:
        """
        Get chat information.

        Args:
            chat_id: Chat ID or username

        Returns:
            Chat information
        """
        data = await self._request("getChat", {"chat_id": chat_id})
        return Chat.from_dict(data)

    async def get_chat_member_count(self, chat_id: Union[int, str]) -> int:
        """
        Get member count in chat.

        Args:
            chat_id: Chat ID or username

        Returns:
            Number of members
        """
        data = await self._request("getChatMemberCount", {"chat_id": chat_id})
        return int(data)

    async def get_chat_member(self, chat_id: Union[int, str], user_id: int) -> Dict[str, Any]:
        """
        Get chat member information.

        Args:
            chat_id: Chat ID or username
            user_id: User ID

        Returns:
            Chat member information
        """
        return await self._request("getChatMember", {"chat_id": chat_id, "user_id": user_id})

    async def get_chat_permissions(self, chat_id: int | str) -> dict[str, bool]:
        """Return the current default member permissions for a chat."""
        data = await self._request("getChat", {"chat_id": chat_id})
        permissions = data.get("permissions") if isinstance(data, dict) else None
        return dict(permissions) if isinstance(permissions, dict) else {}

    async def set_chat_permissions(
        self,
        chat_id: int | str,
        permissions: dict[str, bool],
        *,
        use_independent_chat_permissions: bool = True,
    ) -> bool:
        """Set default permissions applied to all non-administrator members."""
        result = await self._request(
            "setChatPermissions",
            {
                "chat_id": chat_id,
                "permissions": permissions,
                "use_independent_chat_permissions": use_independent_chat_permissions,
            },
        )
        return bool(result)

    # =============================================================================
    # Message Operations
    # =============================================================================

    async def send_message(
        self,
        chat_id: Union[int, str],
        text: str,
        parse_mode: Optional[str] = "Markdown",
        disable_web_page_preview: bool = False,
        disable_notification: bool = False,
        reply_to_message_id: int = None,
        reply_markup: Dict = None,
    ) -> Message:
        """
        Send message to chat.

        Args:
            chat_id: Chat ID or username
            text: Message text
            parse_mode: Parse mode (Markdown, HTML)
            disable_web_page_preview: Disable link previews
            disable_notification: Send silently
            reply_to_message_id: Reply to message ID
            reply_markup: Inline keyboard markup

        Returns:
            Sent message
        """
        await self._rate_limiter.acquire("message")
        params = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
            "disable_notification": disable_notification,
        }

        if parse_mode:
            params["parse_mode"] = parse_mode
        if reply_to_message_id:
            params["reply_to_message_id"] = reply_to_message_id
        if reply_markup:
            params["reply_markup"] = reply_markup

        async with self._risk_operation(
            AccountRiskAction.BOT_MESSAGE,
            target_type="chat",
            target_id=chat_id,
            details={"source": "bot_api_send_message", "content": text},
        ):
            data = await self._request("sendMessage", params)
        return Message.from_dict(data)

    async def send_photo(
        self,
        chat_id: Union[int, str],
        photo: Union[str, bytes],
        caption: str = None,
        parse_mode: str = "Markdown",
    ) -> Message:
        """
        Send photo to chat.

        Args:
            chat_id: Chat ID or username
            photo: Photo URL or file ID
            caption: Photo caption
            parse_mode: Parse mode

        Returns:
            Sent message
        """
        await self._rate_limiter.acquire("message")
        params = {
            "chat_id": chat_id,
            "photo": photo,
        }

        if caption:
            params["caption"] = caption
            params["parse_mode"] = parse_mode

        async with self._risk_operation(
            AccountRiskAction.BOT_MESSAGE,
            target_type="chat",
            target_id=chat_id,
            details={"source": "bot_api_send_photo", "content": caption},
        ):
            data = await self._request("sendPhoto", params)
        return Message.from_dict(data)

    async def send_document(
        self,
        chat_id: Union[int, str],
        document: Union[str, bytes],
        caption: str = None,
    ) -> Message:
        """
        Send document to chat.

        Args:
            chat_id: Chat ID or username
            document: Document URL or file ID
            caption: Document caption

        Returns:
            Sent message
        """
        await self._rate_limiter.acquire("message")
        params = {
            "chat_id": chat_id,
            "document": document,
        }

        if caption:
            params["caption"] = caption

        async with self._risk_operation(
            AccountRiskAction.BOT_MESSAGE,
            target_type="chat",
            target_id=chat_id,
            details={"source": "bot_api_send_document", "content": caption},
        ):
            data = await self._request("sendDocument", params)
        return Message.from_dict(data)

    async def delete_message(self, chat_id: Union[int, str], message_id: int) -> bool:
        """
        Delete message.

        Args:
            chat_id: Chat ID or username
            message_id: Message ID

        Returns:
            True if successful
        """
        result = await self._request(
            "deleteMessage", {"chat_id": chat_id, "message_id": message_id}
        )
        return bool(result)

    async def pin_chat_message(
        self,
        chat_id: Union[int, str],
        message_id: int,
        disable_notification: bool = True,
    ) -> bool:
        """
        Pin a message in a chat.

        Args:
            chat_id: Chat ID or username
            message_id: Message ID to pin
            disable_notification: Pin silently

        Returns:
            True if successful
        """
        if self.risk_guard is not None:
            decision = await self.risk_guard.check_and_reserve(
                self.risk_account,
                AccountRiskAction.BOT_PIN,
                target_type="message",
                target_id=message_id,
                details={"chat_id": chat_id, "source": "bot_api_pin"},
            )
            if not decision.allowed:
                raise RateLimitError(f"risk_guard_blocked:{decision.reason}")
        try:
            result = await self._request(
                "pinChatMessage",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "disable_notification": disable_notification,
                },
            )
            if self.risk_guard is not None:
                await self.risk_guard.record_success(
                    self.risk_account,
                    AccountRiskAction.BOT_PIN,
                    target_type="message",
                    target_id=message_id,
                    details={"chat_id": chat_id, "source": "bot_api_pin"},
                )
            return bool(result)
        except Exception as exc:
            if self.risk_guard is not None:
                await self.risk_guard.record_failure(
                    self.risk_account,
                    AccountRiskAction.BOT_PIN,
                    exc,
                    target_type="message",
                    target_id=message_id,
                    details={"chat_id": chat_id, "source": "bot_api_pin"},
                )
            raise

    async def edit_message_text(
        self,
        chat_id: Union[int, str],
        message_id: int,
        text: str,
        parse_mode: str = "Markdown",
    ) -> Message:
        """
        Edit message text.

        Args:
            chat_id: Chat ID or username
            message_id: Message ID
            text: New text
            parse_mode: Parse mode

        Returns:
            Updated message
        """
        data = await self._request(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": parse_mode,
            },
        )
        return Message.from_dict(data)

    # =============================================================================
    # Member Management
    # =============================================================================

    async def ban_chat_member(
        self,
        chat_id: Union[int, str],
        user_id: int,
        until_date: int = None,
    ) -> bool:
        """
        Ban user from chat.

        Args:
            chat_id: Chat ID or username
            user_id: User ID
            until_date: Ban until timestamp

        Returns:
            True if successful
        """
        params = {"chat_id": chat_id, "user_id": user_id}
        if until_date:
            params["until_date"] = until_date

        result = await self._request("banChatMember", params)
        return bool(result)

    async def unban_chat_member(
        self,
        chat_id: Union[int, str],
        user_id: int,
        only_if_banned: bool = True,
    ) -> bool:
        """
        Unban user from chat.

        Args:
            chat_id: Chat ID or username
            user_id: User ID
            only_if_banned: Only if user is banned

        Returns:
            True if successful
        """
        result = await self._request(
            "unbanChatMember",
            {"chat_id": chat_id, "user_id": user_id, "only_if_banned": only_if_banned},
        )
        return bool(result)

    async def restrict_chat_member(
        self,
        chat_id: Union[int, str],
        user_id: int,
        permissions: Dict[str, bool],
        until_date: int = None,
    ) -> bool:
        """
        Restrict user permissions in chat.

        Args:
            chat_id: Chat ID or username
            user_id: User ID
            permissions: Permission dict
            until_date: Restriction until timestamp

        Returns:
            True if successful
        """
        params = {
            "chat_id": chat_id,
            "user_id": user_id,
            "permissions": permissions,
        }
        if until_date:
            params["until_date"] = until_date

        result = await self._request("restrictChatMember", params)
        return bool(result)

    async def promote_chat_member(
        self,
        chat_id: Union[int, str],
        user_id: int,
        is_anonymous: bool = False,
        can_change_info: bool = True,
        can_post_messages: bool = None,
        can_edit_messages: bool = None,
        can_delete_messages: bool = True,
        can_invite_users: bool = True,
        can_restrict_members: bool = True,
        can_pin_messages: bool = None,
        can_promote_members: bool = False,
    ) -> bool:
        """
        Promote user to admin in chat.

        Args:
            chat_id: Chat ID or username
            user_id: User ID
            is_anonymous: Anonymous admin
            can_change_info: Can change chat info
            can_post_messages: Can post messages (channels)
            can_edit_messages: Can edit messages (channels)
            can_delete_messages: Can delete messages
            can_invite_users: Can invite users
            can_restrict_members: Can restrict members
            can_pin_messages: Can pin messages
            can_promote_members: Can promote members

        Returns:
            True if successful
        """
        params = {
            "chat_id": chat_id,
            "user_id": user_id,
            "is_anonymous": is_anonymous,
            "can_change_info": can_change_info,
            "can_delete_messages": can_delete_messages,
            "can_invite_users": can_invite_users,
            "can_restrict_members": can_restrict_members,
            "can_promote_members": can_promote_members,
        }

        # Only add if not None
        for key, value in [
            ("can_post_messages", can_post_messages),
            ("can_edit_messages", can_edit_messages),
            ("can_pin_messages", can_pin_messages),
        ]:
            if value is not None:
                params[key] = value

        result = await self._request("promoteChatMember", params)
        return bool(result)

    # =============================================================================
    # User Information
    # =============================================================================

    async def get_user_profile_photos(
        self, user_id: int, offset: int = 0, limit: int = 100
    ) -> List[Dict]:
        """
        Get user profile photos.

        Args:
            user_id: User ID
            offset: Offset
            limit: Limit

        Returns:
            List of photos
        """
        return await self._request(
            "getUserProfilePhotos", {"user_id": user_id, "offset": offset, "limit": limit}
        )

    # =============================================================================
    # File Operations
    # =============================================================================

    async def get_file(self, file_id: str) -> Dict[str, Any]:
        """
        Get file information.

        Args:
            file_id: File ID

        Returns:
            File information with download path
        """
        return await self._request("getFile", {"file_id": file_id})

    def get_file_url(self, file_path: str) -> str:
        """
        Get file download URL.

        Args:
            file_path: File path from getFile

        Returns:
            Download URL
        """
        if not self.config.bot_token:
            raise TelegramAPIError("Bot token not configured")
        return f"https://api.telegram.org/file/bot{self.config.bot_token}/{file_path}"

    # =============================================================================
    # Group Search (using exported chat invite links)
    # =============================================================================

    async def search_public_groups(self, query: str, limit: int = 20) -> List[Chat]:
        """
        Search for public groups.

        Note: Bot API doesn't support direct search.
        This uses workaround via inline bots or cached data.

        Args:
            query: Search query
            limit: Max results

        Returns:
            List of matching chats
        """
        # Method 1: Use getUpdates to monitor join requests
        # This requires bot to be admin in the groups

        # Method 2: Query via stored invite links
        # For now, return empty list as proper implementation
        # requires additional infrastructure

        self.logger.info("search_groups_not_implemented", query=query)
        return []

    # =============================================================================
    # Inline Mode
    # =============================================================================

    async def answer_inline_query(
        self,
        inline_query_id: str,
        results: List[Dict],
        cache_time: int = 300,
    ) -> bool:
        """
        Answer inline query.

        Args:
            inline_query_id: Inline query ID
            results: List of inline results
            cache_time: Cache time in seconds

        Returns:
            True if successful
        """
        result = await self._request(
            "answerInlineQuery",
            {
                "inline_query_id": inline_query_id,
                "results": results,
                "cache_time": cache_time,
            },
        )
        return bool(result)


class RateLimiter:
    """Simple rate limiter using sliding window."""

    def __init__(self):
        self._timestamps: Dict[str, List[float]] = {}

    async def acquire(self, key: str, limit: int = 30, window: float = 1.0):
        """
        Acquire rate limit slot.

        Args:
            key: Rate limit key
            limit: Max requests per window
            window: Time window in seconds
        """
        now = time.time()

        if key not in self._timestamps:
            self._timestamps[key] = []

        # Remove old timestamps
        self._timestamps[key] = [ts for ts in self._timestamps[key] if now - ts < window]

        # Check limit
        if len(self._timestamps[key]) >= limit:
            sleep_time = window - (now - self._timestamps[key][0])
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

        # Add new timestamp
        self._timestamps[key].append(time.time())


# =============================================================================
# Singleton Client
# =============================================================================

_telegram_client: Optional[TelegramClient] = None


def get_telegram_client() -> TelegramClient:
    """Get singleton Telegram client."""
    global _telegram_client
    if _telegram_client is None:
        from app.core.config import get_settings

        settings = get_settings()
        config = TelegramConfig(
            bot_token=settings.BOT_TOKEN if hasattr(settings, "BOT_TOKEN") else None,
        )
        _telegram_client = TelegramClient(config)
    return _telegram_client


async def init_telegram_client(bot_token: str) -> TelegramClient:
    """Initialize Telegram client with token."""
    global _telegram_client
    if _telegram_client:
        await _telegram_client.close()

    config = TelegramConfig(bot_token=bot_token)
    _telegram_client = TelegramClient(config)

    # Verify token works
    try:
        await _telegram_client.get_me()
    except TelegramAPIError as e:
        _telegram_client = None
        raise e

    return _telegram_client


async def close_telegram_client():
    """Close Telegram client."""
    global _telegram_client
    if _telegram_client:
        await _telegram_client.close()
        _telegram_client = None
