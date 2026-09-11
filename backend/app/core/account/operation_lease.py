"""Redis-backed leases for cross-process Telegram account operations."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog

from app.core.redis import RedisCache
from app.modules.owned_group.security import safe_exception_message

logger = structlog.get_logger()

_REFRESH_LEASE_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("EXPIRE", KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE_LEASE_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
end
return 0
"""


class AccountOperationLeaseUnavailable(RuntimeError):
    """Raised when a required distributed lease cannot be checked safely."""


class AccountOperationLeaseBusy(RuntimeError):
    """Raised when another operation currently owns an account lease."""


@dataclass(frozen=True)
class AccountOperationLeaseHandle:
    account_id: int
    key: str
    token: str
    owner: str
    ttl_seconds: int


class AccountOperationLeaseManager:
    """Coordinate one active Telegram operation per account across processes."""

    def __init__(
        self,
        cache: RedisCache | None = None,
        *,
        key_prefix: str = "vanguard:account-operation",
    ):
        self.cache = cache
        self._uses_global_client = cache is None
        self.key_prefix = key_prefix.rstrip(":")
        self.logger = logger.bind(module="account_operation_lease")

    def _client(self):
        if not self._uses_global_client:
            return self.cache.client if self.cache is not None else None

        # Celery creates a fresh asyncio loop for every task. Never retain the
        # previous loop's Redis client in a long-lived account-pool singleton.
        from app.core import redis as redis_module

        return redis_module.redis_client

    async def acquire(
        self,
        account_id: int,
        *,
        owner: str,
        ttl_seconds: int = 600,
    ) -> AccountOperationLeaseHandle | None:
        client = self._client()
        if client is None:
            raise AccountOperationLeaseUnavailable("Redis 未初始化，无法安全锁定账号")

        ttl = max(30, int(ttl_seconds))
        key = f"{self.key_prefix}:{int(account_id)}"
        token = f"{owner}:{uuid.uuid4().hex}"
        try:
            acquired = await client.set(key, token, nx=True, ex=ttl)
        except Exception as exc:
            raise AccountOperationLeaseUnavailable(f"Redis 账号锁不可用: {exc}") from exc
        if not acquired:
            return None
        return AccountOperationLeaseHandle(
            account_id=int(account_id),
            key=key,
            token=token,
            owner=owner,
            ttl_seconds=ttl,
        )

    async def refresh(self, handle: AccountOperationLeaseHandle) -> bool:
        client = self._client()
        if client is None:
            raise AccountOperationLeaseUnavailable("Redis 未初始化，无法续期账号锁")
        try:
            result = await client.eval(
                _REFRESH_LEASE_LUA,
                1,
                handle.key,
                handle.token,
                handle.ttl_seconds,
            )
        except Exception as exc:
            raise AccountOperationLeaseUnavailable(f"Redis 账号锁续期失败: {exc}") from exc
        return bool(result)

    async def release(self, handle: AccountOperationLeaseHandle | None) -> bool:
        if handle is None:
            return False
        client = self._client()
        if client is None:
            return False
        try:
            result = await client.eval(_RELEASE_LEASE_LUA, 1, handle.key, handle.token)
        except Exception as exc:
            self.logger.warning(
                "account_operation_lease_release_failed",
                account_id=handle.account_id,
                owner=handle.owner,
                error=safe_exception_message(exc, max_length=500),
            )
            return False
        return bool(result)
