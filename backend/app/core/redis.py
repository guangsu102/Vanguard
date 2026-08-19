"""
Redis Connection and Cache Management

Async Redis client with connection pooling and cache utilities.
"""

from typing import Any, Optional

import redis.asyncio as redis
from redis.asyncio import Redis

from app.core.config import settings


# Global Redis client
redis_client: Redis | None = None


async def init_redis() -> None:
    """Initialize Redis connection."""
    global redis_client
    
    redis_client = redis.from_url(
        settings.REDIS_URL,
        password=settings.effective_redis_password,
        encoding="utf-8",
        decode_responses=True,
        max_connections=50,
    )


async def close_redis() -> None:
    """Close Redis connection."""
    global redis_client
    if redis_client:
        await redis_client.close()
        redis_client = None


async def get_redis() -> Redis:
    """Get Redis client for dependency injection."""
    if redis_client is None:
        raise RuntimeError("Redis not initialized")
    return redis_client


class RedisCache:
    """Redis cache wrapper with common operations."""
    
    def __init__(self, client: Redis | None = None):
        self.client = client or redis_client
    
    async def get(self, key: str) -> Optional[str]:
        """Get value from cache."""
        if self.client is None:
            return None
        return await self.client.get(key)
    
    async def set(
        self,
        key: str,
        value: str,
        ttl: int | None = None,
    ) -> bool:
        """Set value in cache with optional TTL."""
        if self.client is None:
            return False
        if ttl:
            return await self.client.setex(key, ttl, value)
        return await self.client.set(key, value)
    
    async def delete(self, key: str) -> int:
        """Delete key from cache."""
        if self.client is None:
            return 0
        return await self.client.delete(key)
    
    async def exists(self, key: str) -> bool:
        """Check if key exists."""
        if self.client is None:
            return False
        return await self.client.exists(key) > 0
    
    async def get_json(self, key: str) -> Optional[Any]:
        """Get JSON value from cache."""
        import json
        value = await self.get(key)
        if value:
            return json.loads(value)
        return None
    
    async def set_json(
        self,
        key: str,
        value: Any,
        ttl: int | None = None,
    ) -> bool:
        """Set JSON value in cache."""
        import json
        return await self.set(key, json.dumps(value), ttl)
    
    async def incr(self, key: str, amount: int = 1) -> int:
        """Increment value."""
        if self.client is None:
            return 0
        return await self.client.incrby(key, amount)
    
    async def expire(self, key: str, ttl: int) -> bool:
        """Set expiration on key."""
        if self.client is None:
            return False
        return await self.client.expire(key, ttl)


# Rate limiter using Redis
class RateLimiter:
    """Token bucket rate limiter using Redis."""
    
    def __init__(self, key_prefix: str = "ratelimit:"):
        self.key_prefix = key_prefix
    
    async def check(
        self,
        identifier: str,
        rate: int,
        period: int,
    ) -> bool:
        """
        Check if request is allowed under rate limit.
        
        Args:
            identifier: Unique identifier (e.g., user_id, ip)
            rate: Maximum requests allowed
            period: Time period in seconds
            
        Returns:
            True if allowed, False if rate limited
        """
        import time
        
        if redis_client is None:
            return True
        
        key = f"{self.key_prefix}{identifier}"
        now = int(time.time())
        window_key = f"{key}:{now // period}"
        
        # Use Redis pipeline for atomic operations
        pipe = redis_client.pipeline()
        pipe.incr(window_key)
        pipe.expire(window_key, period)
        results = await pipe.execute()
        
        return results[0] <= rate
