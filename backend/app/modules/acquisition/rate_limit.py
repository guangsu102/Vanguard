"""
Shared rate limiting helpers for acquisition modules.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.core.redis import RateLimiter, RedisCache
from app.modules.acquisition.config import AcquisitionConfig


class AcquisitionRateLimitService:
    """Reusable rate limiting service for acquisition workflows."""

    def __init__(
        self,
        key_prefix: str,
        config: Optional[AcquisitionConfig] = None,
        limiter: Optional[RateLimiter] = None,
        cache: Optional[RedisCache] = None,
    ):
        self.config = config or AcquisitionConfig()
        self.limiter = limiter or RateLimiter(key_prefix=key_prefix)
        self.cache = cache or RedisCache()
        self.key_prefix = key_prefix.rstrip(":")

    def build_key(self, *parts: object) -> str:
        """Build a namespaced redis key."""
        suffix = ":".join(str(part) for part in parts if part is not None and str(part) != "")
        return f"{self.key_prefix}:{suffix}" if suffix else self.key_prefix

    async def allow_daily(self, identifier: str, rate: int, period: int = 24 * 3600) -> bool:
        """Check a daily quota for one identifier."""
        return await self.limiter.check(identifier, rate=max(1, rate), period=period)

    async def check_daily_and_cooldown(
        self,
        *,
        daily_key: str,
        cooldown_key: str,
        daily_rate: int,
        cooldown_seconds: int,
        daily_period: int = 24 * 3600,
    ) -> bool:
        """Check a daily cap and a cooldown window."""
        allowed = await self.allow_daily(daily_key, rate=daily_rate, period=daily_period)
        if not allowed:
            return False

        cooldown_seconds = max(1, cooldown_seconds)
        last_at = await self.cache.get(cooldown_key)
        if last_at:
            try:
                elapsed = datetime.utcnow().timestamp() - float(last_at)
                if elapsed < cooldown_seconds:
                    return False
            except ValueError:
                pass

        await self.cache.set(cooldown_key, str(datetime.utcnow().timestamp()), ttl=cooldown_seconds)
        return True
