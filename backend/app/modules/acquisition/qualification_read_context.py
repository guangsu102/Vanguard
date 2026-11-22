"""Bounded read reuse inside one qualification event and client generation.

The caller owns the event boundary: a later event or client generation must use
a new context. Messages enter this context only after an explicit current
``get_messages`` response (or an equally scoped response registered by the
caller). It is not a cross-account visibility or permission cache.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from typing import Any


def _parameter(value: Any) -> Any:
    """Keep exact RPC parameters, including Telegram peer/filter namespaces."""
    if value is None or isinstance(value, (str, int, bool, float, bytes)):
        return type(value).__name__, value
    if isinstance(value, dict):
        return tuple(sorted((str(key), _parameter(item)) for key, item in value.items()))
    if isinstance(value, (tuple, list)):
        return tuple(_parameter(item) for item in value)
    if hasattr(value, "to_dict"):
        return type(value).__name__, _parameter(value.to_dict())
    if hasattr(value, "isoformat"):
        return type(value).__name__, value.isoformat()
    return type(value).__name__, id(value)


def _cache_hit(reason: str, opportunity: Any) -> None:
    """A cache hit is observable; a counterfactual network saving is unknown.

    SDK get_me/get_entity can be local, iterators can span multiple RPCs and
    batch subsets overlap. Without an exact original-operation measurement,
    none of these hits is claimed as a number of avoided Telegram requests.
    """
    try:
        from app.core.account.read_metrics import mark_optimization
    except ImportError:
        return
    mark_optimization(reason, "used")


def _entity_key(entity: Any) -> tuple[str, Any]:
    """Keep Telegram namespaces separate even when numeric IDs coincide."""
    for field, kind in (("channel_id", "channel"), ("chat_id", "chat"), ("user_id", "user")):
        value = getattr(entity, field, None)
        if value is not None:
            return kind, value
    identifier = getattr(entity, "id", None)
    name = type(entity).__name__
    if hasattr(entity, "megagroup") or hasattr(entity, "broadcast"):
        return "channel", identifier
    if name in {"Chat", "ChatForbidden", "ChatEmpty"}:
        return "chat", identifier
    if name in {"User", "UserEmpty"}:
        return "user", identifier
    # Unknown adapters must not lend facts to an unrelated Telegram namespace.
    return name, identifier if identifier is not None else str(entity)


class QualificationReadContext:
    """One bounded, short-lived event/client scope; never a persisted permission."""

    def __init__(self, *, max_messages: int = 512, max_roles: int = 128,
                 enabled: bool = True, ttl_seconds: float = 300,
                 failure_retry_seconds: float = 60) -> None:
        self.enabled = enabled
        self.ttl_seconds = max(0.001, min(300, ttl_seconds))
        self.failure_retry_seconds = max(0, failure_retry_seconds)
        self._started = time.monotonic()
        self.max_messages = max(1, max_messages)
        self.max_roles = max(1, max_roles)
        self._messages: OrderedDict[tuple[Any, ...], Any] = OrderedDict()
        self._roles: OrderedDict[tuple[Any, ...], str] = OrderedDict()
        self._clients: dict[int, Any] = {}
        self._values: OrderedDict[tuple[Any, ...], Any] = OrderedDict()
        self._failures: OrderedDict[tuple[Any, ...], tuple[float, Exception]] = OrderedDict()
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        """A new event, membership or authorization generation needs new facts."""
        self._messages.clear()
        self._roles.clear()
        self._values.clear()
        self._failures.clear()
        self._clients.clear()
        self._started = time.monotonic()

    def _fresh(self) -> None:
        if time.monotonic() - self._started >= self.ttl_seconds:
            self.invalidate()

    def _client_key(self, client: Any) -> tuple[Any, ...]:
        self._fresh()
        self._clients[id(client)] = client
        # Telethon replaces its sender on a new connection generation. Holding
        # both objects prevents Python ID reuse lending old visibility/rights.
        sender = getattr(client, "_sender", None)
        connection = getattr(sender, "_connection", None)
        if sender is not None:
            self._clients[id(sender)] = sender
        if connection is not None:
            self._clients[id(connection)] = connection
        return (id(client), id(sender) if sender is not None else None,
                id(connection) if connection is not None else None)

    async def _invoke(self, key: tuple[Any, ...], call: Callable[[], Awaitable[Any]]) -> Any:
        self._fresh()
        failed = self._failures.get(key) if self.enabled else None
        if failed and failed[0] > time.monotonic():
            _cache_hit("failure_backoff", (id(self), key))
            raise failed[1].with_traceback(None)
        self._failures.pop(key, None)
        try:
            return await call()
        except Exception as exc:
            delay = getattr(exc, "retry_after_seconds", None)
            if delay is None:
                delay = getattr(exc, "seconds", self.failure_retry_seconds)
            if self.enabled and isinstance(delay, (int, float)) and delay > 0:
                self._failures[key] = (min(self._started + self.ttl_seconds,
                                           time.monotonic() + delay), exc)
                while len(self._failures) > self.max_roles:
                    self._failures.popitem(last=False)
            raise

    async def _read(self, client: Any, operation: str, parameters: Any,
                    call: Callable[[], Awaitable[Any]]) -> Any:
        if not self.enabled:
            return await call()
        async with self._lock:
            key = (*self._client_key(client), operation, _parameter(parameters))
            if key in self._values:
                self._values.move_to_end(key)
                _cache_hit("same_work", (id(self), key))
                return self._values[key]
            value = await self._invoke(key, call)
            self._values[key] = value
            while len(self._values) > self.max_roles:
                self._values.popitem(last=False)
            return value

    async def get_entity(self, client: Any, target: Any) -> Any:
        return await self._read(client, "entity", target, lambda: client.get_entity(target))

    async def get_me(self, client: Any, *, input_peer: bool = False) -> Any:
        return await self._read(client, "me", input_peer,
                                lambda: client.get_me(input_peer=True) if input_peer else client.get_me())

    async def full_info(self, client: Any, entity: Any) -> Any:
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.functions.messages import GetFullChatRequest

        request = GetFullChannelRequest(entity) if hasattr(entity, "megagroup") else GetFullChatRequest(entity.id)
        return await self._read(client, "full_info", _entity_key(entity), lambda: client(request))

    async def permissions(self, client: Any, entity: Any, user: Any, *, full_chat: Any = None,
                          direct: bool = False) -> Any:
        from app.modules.acquisition.qualification_reads import current_permissions

        identity = _entity_key(user) if not isinstance(user, (str, int)) else _parameter(user)
        return await self._read(client, "permissions", (_entity_key(entity), identity, id(full_chat), direct),
                                lambda: client.get_permissions(entity, user) if direct else
                                current_permissions(client, entity, user, full_chat=full_chat))

    async def onlines(self, client: Any, entity: Any) -> Any:
        from telethon.tl.functions.messages import GetOnlinesRequest

        return await self._read(client, "onlines", _entity_key(entity),
                                lambda: client(GetOnlinesRequest(entity)))

    async def messages(self, client: Any, entity: Any, **kwargs: Any) -> list[Any]:
        async def collect() -> list[Any]:
            return [item async for item in client.iter_messages(entity, **kwargs)]

        return await self._read(client, "iter_messages", (_entity_key(entity), kwargs), collect)

    def _key(self, client: Any, entity: Any, identifier: int) -> tuple[Any, ...]:
        # Holding the client also prevents object-ID reuse during this context.
        return *self._client_key(client), *_entity_key(entity), identifier

    def cached_message(self, client: Any, entity: Any, message_id: int) -> tuple[bool, Any]:
        key = self._key(client, entity, message_id)
        if not self.enabled:
            return False, None
        if key not in self._messages:
            return False, None
        self._messages.move_to_end(key)
        return True, self._messages[key]

    def remember_messages(
        self, client: Any, entity: Any, messages: Any,
        requested_ids: Iterable[int] | None = None,
    ) -> None:
        """Register a fresh targeted response; missing requested IDs are known absent."""
        values = messages if isinstance(messages, (list, tuple)) else [messages]
        if not self.enabled:
            return
        found = {
            message.id: message for message in values
            if message is not None and type(getattr(message, "id", None)) is int
        }
        identifiers = list(dict.fromkeys(requested_ids)) if requested_ids is not None else list(found)
        for identifier in identifiers:
            key = self._key(client, entity, identifier)
            self._messages[key] = found.get(identifier)
            self._messages.move_to_end(key)
        while len(self._messages) > self.max_messages:
            self._messages.popitem(last=False)

    async def get_messages(self, client: Any, entity: Any, ids: Iterable[int],
                           *, require_live: bool = False) -> list[Any]:
        """Batch missing IDs once, preserving unique caller order and missing values."""
        identifiers = list(dict.fromkeys(ids))
        if any(type(identifier) is not int or identifier <= 0 for identifier in identifiers):
            raise ValueError("qualification_message_ids_invalid")
        if not identifiers:
            return []
        if not self.enabled:
            return await client.get_messages(entity, ids=identifiers)
        async with self._lock:
            cached = {
                identifier: self.cached_message(client, entity, identifier)
                for identifier in identifiers
            }
            if require_live:
                cached = {identifier: (False, None) if (getattr(value[1], "_vanguard_event_evidence", False)
                          or getattr(value[1], "_vanguard_history_evidence", False)) else value
                          for identifier, value in cached.items()}
            missing = [identifier for identifier in identifiers if not cached[identifier][0]]
            fetched: dict[int, Any] = {}
            if missing:
                key = (*self._client_key(client), "get_messages", _entity_key(entity), tuple(sorted(missing)))
                response = await self._invoke(key, lambda: client.get_messages(entity, ids=missing))
                values = response if isinstance(response, (list, tuple)) else [response]
                fetched = {
                    message.id: message for message in values
                    if message is not None and type(getattr(message, "id", None)) is int
                }
                self.remember_messages(client, entity, values, requested_ids=missing)
            else:
                reason = "push" if any(getattr(value[1], "_vanguard_event_evidence", False) for value in cached.values()) else "same_work"
                _cache_hit(reason, (id(self), self._client_key(client), _entity_key(entity), tuple(sorted(identifiers))))
            # A large response can exceed the bounded cache; still return every
            # requested result directly from this call without another read.
            return [cached[identifier][1] if cached[identifier][0] else fetched.get(identifier)
                    for identifier in identifiers]

    async def get_message(self, client: Any, entity: Any, message_id: int,
                          *, require_live: bool = False) -> Any:
        """Preserve the SDK's scalar request shape for a single current root."""
        if type(message_id) is not int or message_id <= 0:
            raise ValueError("qualification_message_ids_invalid")
        if not self.enabled:
            return await client.get_messages(entity, ids=message_id)
        async with self._lock:
            present, value = self.cached_message(client, entity, message_id)
            if present and not (require_live and (getattr(value, "_vanguard_event_evidence", False)
                                                 or getattr(value, "_vanguard_history_evidence", False))):
                _cache_hit("push" if getattr(value, "_vanguard_event_evidence", False) else "same_work",
                         (id(self), self._client_key(client), _entity_key(entity), (message_id,)))
                return value
            key = (*self._client_key(client), "get_messages", _entity_key(entity), (message_id,))
            value = await self._invoke(key, lambda: client.get_messages(entity, ids=message_id))
            self.remember_messages(client, entity, value, requested_ids=[message_id])
            return self.cached_message(client, entity, message_id)[1]

    def role(self, client: Any, entity: Any, sender_id: int) -> str | None:
        key = self._key(client, entity, sender_id)
        if not self.enabled:
            return None
        value = self._roles.get(key)
        if value is not None:
            self._roles.move_to_end(key)
        return value

    def remember_role(self, client: Any, entity: Any, sender_id: int, role: str) -> None:
        """Unknown, failed, system and account-specific permissions never propagate."""
        if not self.enabled or role not in {"ordinary", "admin", "bot"}:
            return
        key = self._key(client, entity, sender_id)
        self._roles[key] = role
        self._roles.move_to_end(key)
        while len(self._roles) > self.max_roles:
            self._roles.popitem(last=False)
