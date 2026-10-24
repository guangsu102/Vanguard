"""Shared Telegram read cache and read-lane guard.

The cache is deliberately placed *before* the Telegram execution boundary.  A
caller supplies an ``actor_reader`` callback; the callback is responsible for
submitting a typed read to the owning account Actor.  This module never creates
Telegram clients and never invokes Telegram directly.

P3 keeps two layers of state:

* Redis is the cross-process fresh cache (with the documented short TTLs).
* A small process-local cache retains a bounded stale value so a transient
  Redis/Telegram failure can be reported as ``stale`` rather than silently
  pretending that an old fact is current.

The public methods return :class:`ReadResult` with an explicit freshness state.
Stale and unknown results are useful for scheduling and diagnostics, but callers
must not use them to confirm a send or create an Outbox operation.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from app.core.redis import get_redis

CacheReader = Callable[..., Any]

ENTITY_USERNAME_TTL = 6 * 60 * 60
ENTITY_PEER_TTL = 24 * 60 * 60
ENTITY_ID_TTL = 24 * 60 * 60
GROUP_FACT_TTL = 5 * 60
SURVIVAL_TTL = 2 * 60
QUALIFICATION_TTL = 24 * 60 * 60

# Retain stale local facts only for diagnostics/scheduling.
ENTITY_STALE_SECONDS = 72 * 60 * 60
GROUP_FACT_STALE_SECONDS = 15 * 60
SURVIVAL_STALE_SECONDS = 10 * 60


async def account_actor_reader(account_pool: Any, **payload: Any) -> Any:
    """Submit a typed read to the session owner's serialized execution queue."""
    from app.core.account.actor_reads import submit_read

    return await submit_read(account_pool, **payload)


def make_account_actor_reader(account_pool: Any) -> CacheReader:
    """Return the callback shape consumed by :class:`TelegramReadCache`."""
    async def _reader(**payload: Any) -> Any:
        return await account_actor_reader(account_pool, **payload)

    return _reader


def _component(value: Any) -> str:
    """Return a bounded, deterministic Redis key component."""

    raw = str(value or "").strip()
    if raw.startswith("@"):
        raw = raw[1:]
    # Keep common Telegram identifiers readable while escaping separators and
    # control characters.  A long target is encoded instead of being truncated
    # so two targets cannot collide.
    return quote(raw, safe="@._:-+").lower()[:512]


def normalize_username(value: Any) -> str:
    return _component(value).removeprefix("@")


@dataclass(frozen=True)
class ReadResult:
    """Result returned by every P3 read method."""

    status: str
    data: Any = None
    fetched_at: float | None = None
    source_version: int | str | None = None
    evidence_hash: str | None = None
    error: str | None = None
    cache_key: str | None = None
    source: str = "actor"

    @property
    def usable_for_send(self) -> bool:
        """Only fresh, known data can authorize a write."""

        return self.status == "fresh" and self.error is None

    @classmethod
    def unknown(cls, *, key: str | None = None, error: str | None = None) -> ReadResult:
        return cls(status="unknown", cache_key=key, error=error, source="error")


@dataclass(frozen=True)
class FeatureFlags:
    """Independent P3 rollout switches.

    Values default to off so deploying the code is safe.  Operators can enable
    each lane separately with ``P3_*`` environment variables or by constructing
    this object explicitly in a worker.
    """

    entity_cache: bool = False
    group_fact_cache: bool = False
    survival_cache: bool = False
    qualification_snapshot: bool = False

    @staticmethod
    def _env(name: str, default: bool = False) -> bool:
        value = os.getenv(name)
        if value is None:
            try:
                from app.core.config import settings

                configured = getattr(settings, name, None)
                if configured is not None:
                    return bool(configured)
            except Exception:
                pass
            return default
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}

    @classmethod
    def from_env(cls) -> FeatureFlags:
        return cls(
            entity_cache=cls._env("P3_ENTITY_CACHE"),
            group_fact_cache=cls._env("P3_GROUP_FACT_CACHE"),
            survival_cache=cls._env("P3_SURVIVAL_CACHE"),
            qualification_snapshot=cls._env("P3_QUALIFICATION_SNAPSHOT"),
        )


@dataclass
class _LocalEntry:
    value: ReadResult
    expires_at: float
    stale_until: float


class ResolveUsernameGuard:
    """Per-account token bucket and FloodWait circuit breaker.

    This is a scheduler guard, not a replacement for ``rpc_governor``.  The
    existing account budget remains authoritative and this guard can only defer
    a username lookup earlier.
    """

    def __init__(
        self,
        *,
        burst: int = 5,
        refill_per_second: float = 1.0,
        failure_window: float = 60.0,
        failure_threshold: int = 3,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.burst = max(1, burst)
        self.refill_per_second = max(0.0, refill_per_second)
        self.failure_window = max(1.0, failure_window)
        self.failure_threshold = max(1, failure_threshold)
        self.clock = clock
        self._buckets: dict[int, tuple[float, float]] = {}
        self._failures: dict[int, deque[float]] = defaultdict(deque)
        self._open_until: dict[int, float] = {}
        self._half_open: set[int] = set()

    def _refill(self, account_id: int, now: float) -> float:
        tokens, updated = self._buckets.get(account_id, (float(self.burst), now))
        tokens = min(float(self.burst), tokens + max(0.0, now - updated) * self.refill_per_second)
        self._buckets[account_id] = (tokens, now)
        return tokens

    def allow(self, account_id: int, *, now: float | None = None) -> bool:
        now = self.clock() if now is None else now
        open_until = self._open_until.get(account_id, 0.0)
        if open_until > now:
            return False
        if open_until:
            # One probe is allowed while half-open.  It remains reserved until
            # record_success/record_flood_wait closes the probe.
            if account_id in self._half_open:
                return False
            self._half_open.add(account_id)
            return True
        tokens = self._refill(account_id, now)
        if tokens < 1.0:
            return False
        self._buckets[account_id] = (tokens - 1.0, now)
        return True

    def record_success(self, account_id: int, *, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        recovering = self._open_until.pop(account_id, None) is not None
        self._half_open.discard(account_id)
        failures = self._failures[account_id]
        if recovering:
            failures.clear()
        while failures and failures[0] < now - self.failure_window:
            failures.popleft()
        self._refill(account_id, now)

    def record_flood_wait(
        self,
        account_id: int,
        wait_seconds: int | float = 0,
        *,
        now: float | None = None,
    ) -> float:
        now = self.clock() if now is None else now
        failures = self._failures[account_id]
        cutoff = now - self.failure_window
        while failures and failures[0] < cutoff:
            failures.popleft()
        failures.append(now)
        was_probe = account_id in self._half_open
        self._half_open.discard(account_id)
        if was_probe or len(failures) >= self.failure_threshold:
            self._open_until[account_id] = now + max(60.0, float(wait_seconds or 0))
        return self._open_until.get(account_id, now)

    def record_failure(self, account_id: int) -> None:
        self._half_open.discard(account_id)

    def state(self, account_id: int, *, now: float | None = None) -> dict[str, Any]:
        now = self.clock() if now is None else now
        tokens = self._refill(account_id, now)
        open_until = self._open_until.get(account_id, 0.0)
        return {
            "tokens": tokens,
            "open": open_until > now,
            "open_until": open_until or None,
            "half_open": account_id in self._half_open,
            "failures": len(self._failures.get(account_id, ())),
        }


_DEFAULT_RESOLVE_GUARD = ResolveUsernameGuard()


class TelegramReadCache:
    """Cache Telegram facts while delegating every miss to an account Actor.

    ``actor_reader`` may be either ``reader(kind, account_id, target_key,
    message_key=...)`` or a callable accepting a mapping.  It should return
    the entity/fact value (or a :class:`ReadResult`).  Exceptions become an
    ``unknown`` result and are never retried by this class.
    """

    def __init__(
        self,
        actor_reader: CacheReader,
        *,
        redis_client: Any | None = None,
        flags: FeatureFlags | None = None,
        clock: Callable[[], float] = time.time,
        guard: ResolveUsernameGuard | None = None,
    ) -> None:
        self.actor_reader = actor_reader
        self.redis_client = redis_client
        self.flags = flags or FeatureFlags.from_env()
        self.clock = clock
        self.guard = guard or (_DEFAULT_RESOLVE_GUARD if clock is time.time else ResolveUsernameGuard(clock=clock))
        self._local: dict[str, _LocalEntry] = {}
        self._inflight: dict[str, asyncio.Task[ReadResult]] = {}
        self._lock = asyncio.Lock()
        self._stats: defaultdict[str, int] = defaultdict(int)
        self._invalidations: defaultdict[str, int] = defaultdict(int)

    @staticmethod
    def entity_username_key(account_id: int, username: Any) -> str:
        return f"tg:entity:{account_id}:username:{normalize_username(username)}"

    @staticmethod
    def entity_peer_key(account_id: int, peer_id: Any) -> str:
        return f"tg:entity:{account_id}:peer:{_component(peer_id)}"

    @staticmethod
    def entity_id_key(account_id: int, telegram_id: Any) -> str:
        return f"tg:entity:{account_id}:id:{_component(telegram_id)}"

    @staticmethod
    def group_fact_key(account_id: int, target_key: Any, kind: str) -> str:
        return f"tg:group_fact:{account_id}:{_component(target_key)}:{kind}"

    @staticmethod
    def survival_key(account_id: int, target_key: Any, message_key: Any) -> str:
        return f"tg:survival:{account_id}:{_component(target_key)}:{_component(message_key)}"

    @staticmethod
    def qualification_key(target_key: Any, group_version: Any, rule_version: Any, evidence_hash: Any) -> str:
        return (
            f"growth:qualification:{_component(target_key)}:{_component(group_version)}:"
            f"{_component(rule_version)}:{_component(evidence_hash)}"
        )

    async def _redis(self) -> Any | None:
        if self.redis_client is not None:
            return self.redis_client
        try:
            self.redis_client = await get_redis()
        except Exception:
            return None
        return self.redis_client

    @staticmethod
    def _decode(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    @staticmethod
    def _json_safe(value: Any) -> Any:
        """Keep Redis envelopes useful when Telethon returns rich objects."""

        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Mapping):
            return {str(key): TelegramReadCache._json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [TelegramReadCache._json_safe(item) for item in value]
        attrs = {}
        for name in (
            "id",
            "access_hash",
            "username",
            "title",
            "first_name",
            "last_name",
            "megagroup",
            "gigagroup",
            "broadcast",
            "has_left",
            "is_banned",
            "is_member",
            "is_admin",
            "is_creator",
            "send_messages",
            "send_media",
        ):
            if hasattr(value, name):
                attrs[name] = TelegramReadCache._json_safe(getattr(value, name))
        return attrs or str(value)

    async def _redis_get(self, key: str) -> Mapping[str, Any] | None:
        client = await self._redis()
        if client is None:
            return None
        try:
            value = await client.get(key)
            value = self._decode(value)
            return value if isinstance(value, Mapping) else None
        except Exception:
            self._stats["redis_errors"] += 1
            return None

    async def _redis_set(self, key: str, value: Mapping[str, Any], ttl: int) -> None:
        client = await self._redis()
        if client is None:
            return
        try:
            encoded = json.dumps(
                self._json_safe(value), ensure_ascii=False, separators=(",", ":"), default=str
            )
            if hasattr(client, "setex"):
                await client.setex(key, ttl, encoded)
            else:
                await client.set(key, encoded, ex=ttl)
        except Exception:
            self._stats["redis_errors"] += 1

    async def _redis_delete(self, keys: list[str]) -> None:
        if not keys:
            return
        client = await self._redis()
        if client is None:
            return
        try:
            await client.delete(*keys)
        except Exception:
            self._stats["redis_errors"] += 1

    @staticmethod
    def _result_from_value(value: Any, *, key: str, now: float) -> ReadResult:
        if isinstance(value, ReadResult):
            return ReadResult(
                status=value.status,
                data=value.data,
                fetched_at=value.fetched_at or now,
                source_version=value.source_version,
                evidence_hash=value.evidence_hash,
                error=value.error,
                cache_key=key,
                source=value.source,
            )
        if isinstance(value, Mapping) and "status" in value and "data" in value:
            return ReadResult(
                status=str(value.get("status") or "fresh"),
                data=value.get("data"),
                fetched_at=float(value.get("fetched_at") or now),
                source_version=value.get("source_version"),
                evidence_hash=value.get("evidence_hash"),
                error=value.get("error"),
                cache_key=key,
                source=str(value.get("source") or "actor"),
            )
        if isinstance(value, Mapping) and "data" in value:
            return ReadResult(
                status=str(value.get("status") or "fresh"),
                data=value.get("data"),
                fetched_at=float(value.get("fetched_at") or now),
                source_version=value.get("source_version"),
                evidence_hash=value.get("evidence_hash"),
                error=value.get("error"),
                cache_key=key,
                source=str(value.get("source") or "actor"),
            )
        source_version = value.get("source_version") if isinstance(value, Mapping) else None
        evidence_hash = value.get("evidence_hash") if isinstance(value, Mapping) else None
        return ReadResult(
            status="fresh",
            data=value,
            fetched_at=now,
            source_version=source_version,
            evidence_hash=evidence_hash,
            cache_key=key,
            source="actor",
        )

    @staticmethod
    def _fresh_envelope(result: ReadResult) -> dict[str, Any]:
        return {
            "status": "fresh",
            "data": result.data,
            "fetched_at": result.fetched_at,
            "source_version": result.source_version,
            "evidence_hash": result.evidence_hash,
            "source": result.source,
        }

    @staticmethod
    def _covers_version(source: int | str | None, required: int | str | None) -> bool:
        if required is None:
            return True
        if source is None:
            return False
        try:
            return int(source) >= int(required)
        except (TypeError, ValueError):
            return str(source) == str(required)

    async def _cached(
        self,
        key: str,
        *,
        local_fact_version: int | str | None = None,
        ttl: int = QUALIFICATION_TTL,
    ) -> ReadResult | None:
        now = self.clock()
        entry = self._local.get(key)
        # Redis is the shared freshness authority: another process may have
        # invalidated a key even though this process still has a local value.
        envelope = await self._redis_get(key)
        if envelope is None or not 0 <= now - float(envelope.get("fetched_at") or 0) < ttl:
            if entry and entry.stale_until > now:
                self._stats["stale_hits"] += 1
                return ReadResult(**{**entry.value.__dict__, "status": "stale", "source": "local"})
            self._stats["misses"] += 1
            return None
        fetched_at = float(envelope.get("fetched_at") or now)
        source_version = envelope.get("source_version")
        if not self._covers_version(source_version, local_fact_version):
            self._stats["version_misses"] += 1
            return None
        if entry and entry.value.fetched_at == fetched_at and entry.value.source_version == source_version:
            self._stats["local_hits"] += 1
            return ReadResult(**{**entry.value.__dict__, "source": "local"})
        result = ReadResult(
            status="fresh",
            data=envelope.get("data"),
            fetched_at=fetched_at,
            source_version=source_version,
            evidence_hash=envelope.get("evidence_hash"),
            cache_key=key,
            source="redis",
        )
        self._stats["redis_hits"] += 1
        return result

    async def _invoke_reader(
        self,
        kind: str,
        account_id: int,
        target_key: str,
        *,
        message_key: str | None = None,
        force_refresh: bool = False,
    ) -> Any:
        payload = {
            "kind": kind,
            "account_id": account_id,
            "target_key": target_key,
            "message_key": message_key,
            "force_refresh": force_refresh,
        }
        try:
            result = self.actor_reader(**payload)
        except TypeError:
            # Support the compact callback form used by worker adapters.
            try:
                result = self.actor_reader(kind, account_id, target_key, message_key)
            except TypeError:
                result = self.actor_reader(payload)
        if inspect.isawaitable(result):
            return await result
        return result

    @staticmethod
    def _wait_seconds(error: BaseException) -> int:
        for name in ("seconds", "retry_after_seconds", "wait_seconds"):
            value = getattr(error, name, None)
            if value is not None:
                try:
                    return max(0, int(value))
                except (TypeError, ValueError):
                    pass
        return 0

    async def _read(
        self,
        *,
        kind: str,
        account_id: int,
        target_key: str,
        key: str,
        ttl: int,
        stale_seconds: int,
        enabled: bool,
        force_refresh: bool = False,
        message_key: str | None = None,
        local_fact_version: int | str | None = None,
    ) -> ReadResult:
        if enabled and not force_refresh:
            cached = await self._cached(key, local_fact_version=local_fact_version, ttl=ttl)
            if cached is not None and cached.status == "fresh":
                return cached
            stale = cached if cached is not None and cached.status == "stale" else None
        else:
            stale = None

        async with self._lock:
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._load(
                        kind=kind,
                        account_id=account_id,
                        target_key=target_key,
                        key=key,
                        ttl=ttl,
                        stale_seconds=stale_seconds,
                        enabled=enabled,
                        force_refresh=force_refresh,
                        message_key=message_key,
                        stale=stale,
                        local_fact_version=local_fact_version,
                    )
                )
                self._inflight[key] = task
                task.add_done_callback(lambda done: self._inflight.pop(key, None) if self._inflight.get(key) is done else None)
        # Cancelling one caller must not cancel the provider read shared by
        # other waiters, nor remove it while it is still in flight.
        result = await asyncio.shield(task)
        if result.status == "fresh" and not self._covers_version(result.source_version, local_fact_version):
            return ReadResult.unknown(key=key, error="telegram_fact_version_unknown_or_stale")
        return result

    async def _load(
        self,
        *,
        kind: str,
        account_id: int,
        target_key: str,
        key: str,
        ttl: int,
        stale_seconds: int,
        enabled: bool,
        force_refresh: bool,
        message_key: str | None,
        stale: ReadResult | None,
        local_fact_version: int | str | None,
    ) -> ReadResult:
        if kind == "entity" and not self.guard.allow(account_id):
            self._stats["resolve_deferred"] += 1
            return stale or ReadResult.unknown(key=key, error="resolve_username_circuit_or_rate_limit")
        now = self.clock()
        invalidation_version = self._invalidations[key]
        try:
            value = await self._invoke_reader(
                kind,
                account_id,
                target_key,
                message_key=message_key,
                force_refresh=force_refresh,
            )
            result = self._result_from_value(value, key=key, now=now)
            if self._invalidations[key] != invalidation_version:
                return ReadResult.unknown(key=key, error="telegram_read_invalidated_inflight")
            if result.status in {"unknown", "stale"}:
                return result
            if not self._covers_version(result.source_version, local_fact_version):
                return ReadResult.unknown(key=key, error="telegram_fact_version_unknown_or_stale")
            if kind == "entity":
                self.guard.record_success(account_id)
            if enabled:
                self._local[key] = _LocalEntry(
                    result,
                    expires_at=now + ttl,
                    stale_until=now + ttl + stale_seconds,
                )
                await self._redis_set(key, self._fresh_envelope(result), ttl)
            self._stats["actor_reads"] += 1
            return result
        except Exception as exc:  # noqa: BLE001 - provider errors become unknown facts
            if kind == "entity":
                wait = self._wait_seconds(exc)
                if wait and "flood" in type(exc).__name__.lower():
                    self.guard.record_flood_wait(account_id, wait)
                else:
                    self.guard.record_failure(account_id)
            self._stats["read_errors"] += 1
            if stale is not None:
                return stale
            return ReadResult.unknown(key=key, error=f"{type(exc).__name__}: {exc}")

    async def resolve_entity(
        self,
        account_id: int,
        target_key: Any,
        force_refresh: bool = False,
        *,
        local_fact_version: int | str | None = None,
    ) -> ReadResult:
        normalized = normalize_username(target_key)
        return await self._read(
            kind="entity",
            account_id=account_id,
            target_key=normalized,
            key=self.entity_username_key(account_id, normalized),
            ttl=ENTITY_USERNAME_TTL,
            stale_seconds=ENTITY_STALE_SECONDS,
            enabled=self.flags.entity_cache,
            force_refresh=force_refresh,
            local_fact_version=local_fact_version,
        )

    async def get_membership(
        self,
        account_id: int,
        target_key: Any,
        force_refresh: bool = False,
        *,
        local_fact_version: int | str | None = None,
    ) -> ReadResult:
        target = _component(target_key)
        return await self._read(
            kind="membership",
            account_id=account_id,
            target_key=target,
            key=self.group_fact_key(account_id, target, "membership"),
            ttl=GROUP_FACT_TTL,
            stale_seconds=GROUP_FACT_STALE_SECONDS,
            enabled=self.flags.group_fact_cache,
            force_refresh=force_refresh,
            local_fact_version=local_fact_version,
        )

    async def get_permissions(
        self,
        account_id: int,
        target_key: Any,
        force_refresh: bool = False,
        *,
        local_fact_version: int | str | None = None,
    ) -> ReadResult:
        target = _component(target_key)
        return await self._read(
            kind="permissions",
            account_id=account_id,
            target_key=target,
            key=self.group_fact_key(account_id, target, "permission"),
            ttl=GROUP_FACT_TTL,
            stale_seconds=GROUP_FACT_STALE_SECONDS,
            enabled=self.flags.group_fact_cache,
            force_refresh=force_refresh,
            local_fact_version=local_fact_version,
        )

    async def get_ad_survival(
        self,
        account_id: int,
        target_key: Any,
        message_key: Any,
        force_refresh: bool = False,
        *,
        local_fact_version: int | str | None = None,
    ) -> ReadResult:
        target, message = _component(target_key), _component(message_key)
        return await self._read(
            kind="survival",
            account_id=account_id,
            target_key=target,
            key=self.survival_key(account_id, target, message),
            ttl=SURVIVAL_TTL,
            stale_seconds=SURVIVAL_STALE_SECONDS,
            enabled=self.flags.survival_cache,
            force_refresh=force_refresh,
            message_key=message,
            local_fact_version=local_fact_version,
        )

    async def get_qualification(
        self,
        target_key: Any,
        group_version: Any,
        rule_version: Any,
        evidence_hash: Any,
    ) -> ReadResult:
        """Read a reusable qualification envelope from the shared cache."""

        key = self.qualification_key(target_key, group_version, rule_version, evidence_hash)
        if not self.flags.qualification_snapshot:
            return ReadResult.unknown(key=key, error="qualification_snapshot_cache_disabled")
        cached = await self._cached(key)
        return cached or ReadResult.unknown(key=key, error="qualification_snapshot_cache_miss")

    async def put_qualification(
        self,
        target_key: Any,
        group_version: Any,
        rule_version: Any,
        evidence_hash: Any,
        value: Mapping[str, Any],
    ) -> ReadResult:
        """Publish an immutable qualification envelope for 24 hours."""

        key = self.qualification_key(target_key, group_version, rule_version, evidence_hash)
        now = self.clock()
        result = self._result_from_value(value, key=key, now=now)
        if not self.flags.qualification_snapshot:
            return result
        self._local[key] = _LocalEntry(
            result,
            expires_at=now + QUALIFICATION_TTL,
            stale_until=now + QUALIFICATION_TTL,
        )
        await self._redis_set(key, self._fresh_envelope(result), QUALIFICATION_TTL)
        self._stats["qualification_writes"] += 1
        return result

    async def invalidate(self, event: Mapping[str, Any] | Any) -> int:
        """Invalidate only keys affected by an inbox/Actor fact event."""

        if isinstance(event, Mapping):
            account_id = event.get("account_id")
            target_key = event.get("target_key") or event.get("username") or event.get("group_id")
            kind = str(event.get("kind") or event.get("event_type") or "").lower()
            peer_id = event.get("peer_id")
            telegram_id = event.get("telegram_id")
            message_key = event.get("message_key") or event.get("message_id")
            deleted_ids = event.get("deleted_ids") or event.get("ids") or ()
        else:
            account_id = getattr(event, "account_id", None)
            target_key = getattr(event, "target_key", None) or getattr(event, "username", None)
            kind = str(getattr(event, "kind", None) or getattr(event, "event_type", None) or "").lower()
            peer_id = getattr(event, "peer_id", None)
            telegram_id = getattr(event, "telegram_id", None)
            message_key = getattr(event, "message_key", None) or getattr(event, "message_id", None)
            deleted_ids = getattr(event, "deleted_ids", None) or getattr(event, "ids", None) or ()

        if isinstance(deleted_ids, (str, bytes, int)):
            deleted_ids = (deleted_ids,)

        keys: list[str] = []
        if account_id is not None:
            if target_key is not None:
                target = _component(target_key)
                if any(token in kind for token in ("member", "permission", "group", "chat")):
                    keys.extend(
                        [
                            self.group_fact_key(int(account_id), target, "membership"),
                            self.group_fact_key(int(account_id), target, "permission"),
                        ]
                    )
                if any(token in kind for token in ("message", "survival", "ad", "delete")) and message_key is not None:
                    keys.append(self.survival_key(int(account_id), target, message_key))
                if any(token in kind for token in ("message", "survival", "ad", "delete")):
                    keys.extend(
                        self.survival_key(int(account_id), target, item)
                        for item in deleted_ids
                        if item is not None
                    )
                if not keys or any(token in kind for token in ("username", "entity", "peer", "identity")):
                    keys.append(self.entity_username_key(int(account_id), target))
            if peer_id is not None:
                keys.append(self.entity_peer_key(int(account_id), peer_id))
            if telegram_id is not None:
                keys.append(self.entity_id_key(int(account_id), telegram_id))

        keys = list(dict.fromkeys(keys))
        for key in keys:
            self._invalidations[key] += 1
            self._local.pop(key, None)
        await self._redis_delete(keys)
        self._stats["invalidations"] += len(keys)
        return len(keys)

    def stats(self) -> dict[str, int]:
        return dict(self._stats)


async def invalidate_telegram_read_cache(
    event: Mapping[str, Any] | Any,
    *,
    redis_client: Any | None = None,
) -> int:
    """Invalidate P3 keys from an inbox/Actor event without a Telegram call.

    The inbox worker does not own an Actor callback, so invalidation uses a
    no-op reader and only exercises the Redis/local invalidation path.
    """

    cache = TelegramReadCache(lambda **_: None, redis_client=redis_client)
    return await cache.invalidate(event)


__all__ = [
    "account_actor_reader",
    "ENTITY_ID_TTL",
    "ENTITY_PEER_TTL",
    "ENTITY_USERNAME_TTL",
    "FeatureFlags",
    "GROUP_FACT_TTL",
    "QUALIFICATION_TTL",
    "ReadResult",
    "ResolveUsernameGuard",
    "SURVIVAL_TTL",
    "TelegramReadCache",
    "make_account_actor_reader",
    "invalidate_telegram_read_cache",
    "normalize_username",
]
