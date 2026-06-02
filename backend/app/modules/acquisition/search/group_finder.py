"""
Group Finder Module

Discovers and filters Telegram groups based on criteria.
"""

from dataclasses import dataclass
from typing import Optional

import structlog

from app.core.account.pool import AccountPool
from app.core.network.proxy_pool import ProxyPool
from app.integrations.telegram.client import TelegramAPIError

logger = structlog.get_logger()


@dataclass
class DiscoveredGroup:
    """Discovered group information."""
    group_id: int
    title: str
    username: Optional[str]
    member_count: int
    is_private: bool
    source_keyword: Optional[str] = None


class GroupFinder:
    """
    Group discovery and search functionality.

    Searches for Telegram groups using various methods:
    - Keyword search via Telegram API
    - Direct username lookup
    - Related group discovery
    """

    def __init__(
        self,
        account_pool: AccountPool,
        proxy_pool: Optional[ProxyPool] = None,
    ):
        """
        Initialize GroupFinder.

        Args:
            account_pool: Account pool for API calls
            proxy_pool: Optional proxy pool for requests
        """
        self.account_pool = account_pool
        self.proxy_pool = proxy_pool
        self.logger = logger.bind(module="group_finder")

    async def search_by_keyword(
        self,
        keyword: str,
        limit: int = 20,
        account_id: Optional[int] = None,
    ) -> list[DiscoveredGroup]:
        """
        Search groups by keyword.

        Args:
            keyword: Search keyword
            limit: Maximum results to return

        Returns:
            List of discovered groups
        """
        self.logger.info("search_by_keyword", keyword=keyword, limit=limit)

        # 获取可用账号
        if account_id is not None and hasattr(self.account_pool, "acquire_by_id"):
            account = await self.account_pool.acquire_by_id(account_id, purpose="search")
        else:
            account = await self.account_pool.acquire(purpose="search")

        if account is None:
            self.logger.warning("search_no_account", keyword=keyword, account_id=account_id)
            return []

        try:
            # 使用 Telegram API 搜索
            results = await self._search_via_api(account, keyword, limit)

            discovered = []
            for result in results:
                normalized = self._normalize_search_result(result)
                if not normalized:
                    continue
                group = DiscoveredGroup(
                    group_id=normalized.get("id"),
                    title=normalized.get("title", ""),
                    username=normalized.get("username"),
                    member_count=normalized.get("participants_count", 0),
                    is_private=normalized.get("type") == "channel" and not normalized.get("username"),
                    source_keyword=keyword,
                )
                discovered.append(group)

            self.logger.info("search_completed", keyword=keyword, found=len(discovered))
            return discovered

        finally:
            if account:
                await self.account_pool.release(account)

    async def search_by_username(
        self,
        username: str,
    ) -> Optional[DiscoveredGroup]:
        """
        Search group by username.

        Args:
            username: Group username (with or without @)

        Returns:
            DiscoveredGroup if found, None otherwise
        """
        # 移除 @ 前缀
        if username.startswith("@"):
            username = username[1:]

        self.logger.info("search_by_username", username=username)

        account = await self.account_pool.acquire(purpose="search")

        try:
            result = await self._resolve_username(account, username)

            if result:
                return DiscoveredGroup(
                    group_id=result.get("id"),
                    title=result.get("title", ""),
                    username=result.get("username"),
                    member_count=result.get("participants_count", 0),
                    is_private=result.get("type") == "private",
                    source_keyword=None,
                )

            return None

        finally:
            await self.account_pool.release(account)

    async def get_group_info(
        self,
        group_id: int,
    ) -> Optional[DiscoveredGroup]:
        """
        Get group information by ID.

        Args:
            group_id: Telegram group ID

        Returns:
            DiscoveredGroup if found, None otherwise
        """
        self.logger.info("get_group_info", group_id=group_id)

        account = await self.account_pool.acquire(purpose="info")

        try:
            result = await self._get_entity(account, group_id)

            if result:
                return DiscoveredGroup(
                    group_id=result.get("id"),
                    title=result.get("title", ""),
                    username=result.get("username"),
                    member_count=result.get("participants_count", 0),
                    is_private=result.get("type") == "private",
                    source_keyword=None,
                )

            return None

        finally:
            await self.account_pool.release(account)

    async def _search_via_api(
        self,
        account,
        keyword: str,
        limit: int,
    ) -> list[dict]:
        """
        Perform search via Telegram API.

        Args:
            account: Telegram account to use
            keyword: Search keyword
            limit: Result limit

        Returns:
            List of search results
        """
        client = getattr(account, "client", None)
        if client is None and hasattr(account, "connect"):
            try:
                client = await account.connect()
                account.client = client
            except Exception as exc:
                self.logger.warning("search_via_api_connect_failed", keyword=keyword, error=str(exc))
                return []

        if client is None:
            self.logger.warning("search_via_api_no_client", keyword=keyword)
            return []

        search_call = None
        for attr in ("search_public_groups", "get_dialogs", "iter_dialogs"):
            if hasattr(client, attr):
                search_call = getattr(client, attr)
                break

        if hasattr(client, "__call__"):
            try:
                from telethon import functions

                found = await client(
                    functions.contacts.SearchRequest(
                        q=keyword,
                        limit=limit,
                    )
                )
                chats = getattr(found, "chats", None)
                if chats:
                    return [self._telethon_chat_to_dict(chat) for chat in chats]
            except Exception as exc:
                self.logger.debug("telethon_public_search_failed", keyword=keyword, error=str(exc))

        if search_call is None:
            self.logger.warning("search_via_api_no_support", keyword=keyword)
            return []

        try:
            if search_call.__name__ == "iter_dialogs":
                result = []
                async for dialog in search_call():
                    title = getattr(dialog, "title", "") or ""
                    username = getattr(dialog, "username", None)
                    if keyword.lower() in title.lower() or (username and keyword.lower() in username.lower()):
                        result.append(dialog)
            else:
                result = await search_call(keyword, limit=limit)
        except TelegramAPIError as exc:
            self.logger.warning("search_via_api_failed", keyword=keyword, error=str(exc))
            return []
        except Exception as exc:
            self.logger.warning("search_via_api_error", keyword=keyword, error=str(exc))
            return []

        if isinstance(result, list):
            return result
        if isinstance(result, dict) and "chats" in result:
            return result.get("chats", [])
        return []

    def _normalize_search_result(self, result) -> Optional[dict]:
        """Normalize dict, bot-api Chat, or Telethon chat objects."""
        if result is None:
            return None
        if isinstance(result, dict):
            if "id" not in result and "chat_id" in result:
                result["id"] = result["chat_id"]
            if "participants_count" not in result and "member_count" in result:
                result["participants_count"] = result["member_count"]
            return result
        return self._telethon_chat_to_dict(result)

    def _telethon_chat_to_dict(self, chat) -> dict:
        """Convert a Telethon chat/channel object to the local shape."""
        username = getattr(chat, "username", None)
        chat_id = getattr(chat, "id", 0)
        title = getattr(chat, "title", "") or getattr(chat, "first_name", "") or ""
        participants_count = (
            getattr(chat, "participants_count", None)
            or getattr(chat, "members_count", None)
            or 0
        )
        is_channel = getattr(chat, "broadcast", False) or getattr(chat, "megagroup", False)
        return {
            "id": chat_id,
            "title": title,
            "username": username,
            "participants_count": participants_count,
            "type": "channel" if is_channel else "group",
        }

    async def _resolve_username(
        self,
        account,
        username: str,
    ) -> Optional[dict]:
        """
        Resolve username to entity.

        Args:
            account: Telegram account to use
            username: Username to resolve

        Returns:
            Entity info dict or None
        """
        client = getattr(account, "client", None)
        if client is None and hasattr(account, "connect"):
            try:
                client = await account.connect()
                account.client = client
            except Exception as exc:
                self.logger.warning("resolve_username_connect_failed", username=username, error=str(exc))
                return None

        if client is None:
            self.logger.warning("resolve_username_no_client", username=username)
            return None

        try:
            if hasattr(client, "get_entity"):
                result = await client.get_entity(username)
            elif hasattr(client, "get_chat"):
                result = await client.get_chat(username)
            else:
                self.logger.warning("resolve_username_no_support", username=username)
                return None
        except Exception as exc:
            self.logger.warning("resolve_username_failed", username=username, error=str(exc))
            return None

        return result if isinstance(result, dict) else getattr(result, "__dict__", None)

    async def _get_entity(
        self,
        account,
        group_id: int,
    ) -> Optional[dict]:
        """
        Get entity by ID.

        Args:
            account: Telegram account to use
            group_id: Entity ID

        Returns:
            Entity info dict or None
        """
        client = getattr(account, "client", None)
        if client is None and hasattr(account, "connect"):
            try:
                client = await account.connect()
                account.client = client
            except Exception as exc:
                self.logger.warning("get_entity_connect_failed", group_id=group_id, error=str(exc))
                return None

        if client is None:
            self.logger.warning("get_entity_no_client", group_id=group_id)
            return None

        try:
            if hasattr(client, "get_entity"):
                result = await client.get_entity(group_id)
            elif hasattr(client, "get_chat"):
                result = await client.get_chat(group_id)
            else:
                self.logger.warning("get_entity_no_support", group_id=group_id)
                return None
        except Exception as exc:
            self.logger.warning("get_entity_failed", group_id=group_id, error=str(exc))
            return None

        return result if isinstance(result, dict) else getattr(result, "__dict__", None)

    async def discover_related(
        self,
        group_id: int,
        limit: int = 10,
    ) -> list[DiscoveredGroup]:
        """
        Discover related groups from a source group.

        Args:
            group_id: Source group ID
            limit: Maximum results

        Returns:
            List of related groups
        """
        self.logger.info("discover_related", source_group=group_id, limit=limit)

        client = None
        account = await self.account_pool.acquire(purpose="discover_related")
        try:
            client = getattr(account, "client", None)
            if client is None and account and hasattr(account, "connect"):
                client = await account.connect()
                account.client = client

            if client is None:
                return []

            related = []
            if hasattr(client, "get_dialogs"):
                dialogs = await client.get_dialogs(limit=limit * 5)
                source = str(group_id)
                for dialog in dialogs:
                    title = getattr(dialog, "title", "") or ""
                    username = getattr(dialog, "username", None)
                    if source in title or (username and source in username):
                        related.append(
                            DiscoveredGroup(
                                group_id=getattr(dialog, "id", 0),
                                title=title,
                                username=username,
                                member_count=getattr(dialog, "participants_count", 0) or 0,
                                is_private=not bool(username),
                                source_keyword=None,
                            )
                        )
            return related[:limit]
        finally:
            if account:
                await self.account_pool.release(account)
