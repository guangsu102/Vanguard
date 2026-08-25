"""
Spam Detector

Detects spam patterns including frequency abuse and repeated content.
"""

import asyncio
import re
import time
from dataclasses import dataclass

import structlog

from app.modules.guardian.config import get_guardian_config

logger = structlog.get_logger()


@dataclass
class SpamCheckResult:
    """Result of spam check."""
    is_spam: bool
    spam_type: str
    details: str
    severity: str


class SpamDetector:
    """
    Spam content detector.
    
    Detects spam patterns like:
    - Message frequency abuse
    - Repeated content
    - Link spam
    """
    
    def __init__(self, redis_client=None):
        """
        Initialize SpamDetector.
        
        Args:
            redis_client: Redis client for rate limiting
        """
        self._redis = redis_client
        self._config = get_guardian_config()
        self._local_cache: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self.logger = logger.bind(module="spam_detector")
    
    def set_redis(self, redis_client) -> None:
        """Set Redis client."""
        self._redis = redis_client
    
    async def check_frequency(
        self,
        user_id: int,
        chat_id: int
    ) -> SpamCheckResult:
        """
        Check message frequency for a user.
        
        Args:
            user_id: User ID
            chat_id: Chat/Group ID
            
        Returns:
            SpamCheckResult
        """
        key = f"spam:freq:{chat_id}:{user_id}"
        max_messages = self._config.max_messages_per_minute
        
        try:
            if self._redis:
                current_count = await self._redis.get(key)
                count = int(current_count) if current_count else 0
                
                if count >= max_messages:
                    return SpamCheckResult(
                        is_spam=True,
                        spam_type="frequency",
                        details=f"User sent {count} messages in the last minute",
                        severity="high"
                    )
                
                pipe = self._redis.pipeline()
                pipe.incr(key)
                pipe.expire(key, 60)
                await pipe.execute()
                
            else:
                async with self._lock:
                    if key not in self._local_cache:
                        self._local_cache[key] = {"count": 0, "timestamp": time.time()}
                    
                    cache = self._local_cache[key]
                    
                    if time.time() - cache["timestamp"] > 60:
                        cache["count"] = 0
                        cache["timestamp"] = time.time()
                    
                    cache["count"] += 1
                    
                    if cache["count"] > max_messages:
                        return SpamCheckResult(
                            is_spam=True,
                            spam_type="frequency",
                            details=f"User sent {cache['count']} messages in the last minute",
                            severity="high"
                        )
            
            return SpamCheckResult(
                is_spam=False,
                spam_type="",
                details="",
                severity=""
            )
            
        except Exception as e:
            self.logger.error("frequency_check_failed", error=str(e))
            return SpamCheckResult(
                is_spam=False,
                spam_type="error",
                details=str(e),
                severity="low"
            )
    
    async def check_repeated_content(
        self,
        user_id: int,
        content: str
    ) -> SpamCheckResult:
        """
        Check for repeated content.
        
        Args:
            user_id: User ID
            content: Message content
            
        Returns:
            SpamCheckResult
        """
        if not content or len(content) < 5:
            return SpamCheckResult(
                is_spam=False,
                spam_type="",
                details="",
                severity=""
            )
        
        normalized = self._normalize_content(content)
        key = f"spam:repeat:{user_id}"
        max_repeat = self._config.max_repeated_messages
        
        try:
            if self._redis:
                current = await self._redis.hget(key, normalized)
                count = int(current) if current else 0
                
                if count >= max_repeat:
                    return SpamCheckResult(
                        is_spam=True,
                        spam_type="repeated",
                        details=f"Same content repeated {count} times",
                        severity="medium"
                    )
                
                pipe = self._redis.pipeline()
                pipe.hincrby(key, normalized, 1)
                pipe.expire(key, 300)
                await pipe.execute()
                
            else:
                async with self._lock:
                    if key not in self._local_cache:
                        self._local_cache[key] = {}
                    
                    cache = self._local_cache[key]
                    cache[normalized] = cache.get(normalized, 0) + 1
                    
                    if cache[normalized] > max_repeat:
                        return SpamCheckResult(
                            is_spam=True,
                            spam_type="repeated",
                            details=f"Same content repeated {cache[normalized]} times",
                            severity="medium"
                        )
            
            return SpamCheckResult(
                is_spam=False,
                spam_type="",
                details="",
                severity=""
            )
            
        except Exception as e:
            self.logger.error("repeated_check_failed", error=str(e))
            return SpamCheckResult(
                is_spam=False,
                spam_type="error",
                details=str(e),
                severity="low"
            )
    
    async def check_link_spam(
        self,
        user_id: int,
        content: str
    ) -> SpamCheckResult:
        """
        Check for link spam.
        
        Args:
            user_id: User ID
            content: Message content
            
        Returns:
            SpamCheckResult
        """
        urls = self._extract_urls(content)
        
        if not urls:
            return SpamCheckResult(
                is_spam=False,
                spam_type="",
                details="",
                severity=""
            )
        
        key = f"spam:link:{user_id}"
        threshold = self._config.link_spam_threshold
        
        try:
            if self._redis:
                current = await self._redis.get(key)
                count = int(current) if current else 0
                
                new_count = count + len(urls)
                
                if new_count > threshold:
                    return SpamCheckResult(
                        is_spam=True,
                        spam_type="links",
                        details=f"User sent {new_count} links, threshold is {threshold}",
                        severity="high"
                    )
                
                pipe = self._redis.pipeline()
                pipe.incrby(key, len(urls))
                pipe.expire(key, 300)
                await pipe.execute()
                
            else:
                async with self._lock:
                    cache = self._local_cache.get(key, {"count": 0, "timestamp": time.time()})
                    
                    if time.time() - cache["timestamp"] > 300:
                        cache["count"] = 0
                        cache["timestamp"] = time.time()
                    
                    cache["count"] += len(urls)
                    self._local_cache[key] = cache
                    
                    if cache["count"] > threshold:
                        return SpamCheckResult(
                            is_spam=True,
                            spam_type="links",
                            details=f"User sent {cache['count']} links",
                            severity="high"
                        )
            
            return SpamCheckResult(
                is_spam=False,
                spam_type="",
                details="",
                severity=""
            )
            
        except Exception as e:
            self.logger.error("link_check_failed", error=str(e))
            return SpamCheckResult(
                is_spam=False,
                spam_type="error",
                details=str(e),
                severity="low"
            )
    
    async def check_all(
        self,
        user_id: int,
        chat_id: int,
        content: str
    ) -> list[SpamCheckResult]:
        """
        Run all spam checks.
        
        Args:
            user_id: User ID
            chat_id: Chat ID
            content: Message content
            
        Returns:
            List of spam check results
        """
        results = []
        
        freq_result = await self.check_frequency(user_id, chat_id)
        if freq_result.is_spam:
            results.append(freq_result)
        
        repeat_result = await self.check_repeated_content(user_id, content)
        if repeat_result.is_spam:
            results.append(repeat_result)
        
        link_result = await self.check_link_spam(user_id, content)
        if link_result.is_spam:
            results.append(link_result)
        
        return results
    
    def _extract_urls(self, text: str) -> list[str]:
        """Extract URLs from text."""
        url_pattern = re.compile(
            r'https?://[^\s<>"{}|\\^`\[\]]+',
            re.IGNORECASE
        )
        return url_pattern.findall(text)
    
    def _normalize_content(self, content: str) -> str:
        """Normalize content for comparison."""
        normalized = content.lower().strip()
        normalized = re.sub(r'\s+', ' ', normalized)
        normalized = re.sub(r'[^\w\s]', '', normalized)
        return normalized[:100]
    
    async def reset_user_spam_count(self, user_id: int) -> None:
        """
        Reset spam count for a user.
        
        Args:
            user_id: User ID
        """
        try:
            if self._redis:
                keys = [
                    f"spam:freq:*:{user_id}",
                    f"spam:repeat:{user_id}",
                    f"spam:link:{user_id}"
                ]
                for pattern in keys:
                    await self._redis.delete(*await self._redis.keys(pattern))
            else:
                async with self._lock:
                    keys_to_remove = [
                        k for k in self._local_cache.keys()
                        if k.endswith(f":{user_id}") or k == f"spam:repeat:{user_id}" or k == f"spam:link:{user_id}"
                    ]
                    for key in keys_to_remove:
                        del self._local_cache[key]
                        
        except Exception as e:
            self.logger.error("reset_spam_count_failed", user_id=user_id, error=str(e))
