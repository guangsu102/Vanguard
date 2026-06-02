"""Redis 缓存与队列客户端"""
import json
from typing import Any, Optional

import redis.asyncio as redis
from loguru import logger


class RedisClient:
    """Redis 客户端封装"""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        password: Optional[str] = None,
        db: int = 0,
        ssl: bool = False
    ):
        self.host = host
        self.port = port
        self.password = password
        self.db = db
        self.ssl = ssl
        self._client: Optional[redis.Redis] = None

    async def connect(self):
        """建立连接"""
        self._client = redis.Redis(
            host=self.host,
            port=self.port,
            password=self.password,
            db=self.db,
            ssl=self.ssl,
            decode_responses=True
        )
        await self._client.ping()
        logger.info(f"Redis 连接成功: {self.host}:{self.port}")

    async def close(self):
        """关闭连接"""
        if self._client:
            await self._client.close()
            logger.info("Redis 连接已关闭")

    @property
    def client(self) -> redis.Redis:
        """获取客户端实例"""
        if not self._client:
            raise RuntimeError("Redis 未连接，请先调用 connect()")
        return self._client

    # ============ 通用操作 ============

    async def get(self, key: str) -> Optional[str]:
        """获取值"""
        return await self.client.get(key)

    async def set(
        self,
        key: str,
        value: Any,
        ex: Optional[int] = None,  # 过期秒数
        px: Optional[int] = None,  # 过期毫秒数
    ) -> bool:
        """设置值"""
        if not isinstance(value, str):
            value = json.dumps(value)
        return await self.client.set(key, value, ex=ex, px=px)

    async def setex(self, key: str, seconds: int, value: Any) -> bool:
        """设置带过期时间的值"""
        if not isinstance(value, str):
            value = json.dumps(value)
        return await self.client.setex(key, seconds, value)

    async def delete(self, *keys: str) -> int:
        """删除键"""
        return await self.client.delete(*keys)

    async def exists(self, key: str) -> bool:
        """检查键是否存在"""
        return await self.client.exists(key) > 0

    async def ttl(self, key: str) -> int:
        """获取键的剩余生存时间"""
        return await self.client.ttl(key)

    async def incr(self, key: str, amount: int = 1) -> int:
        """递增"""
        return await self.client.incr(key, amount)

    async def expire(self, key: str, seconds: int) -> bool:
        """设置过期时间"""
        return await self.client.expire(key, seconds)

    # ============ 列表操作 ============

    async def lpush(self, key: str, *values: Any) -> int:
        """左推入"""
        str_values = [str(v) if not isinstance(v, str) else v for v in values]
        return await self.client.lpush(key, *str_values)

    async def rpush(self, key: str, *values: Any) -> int:
        """右推入"""
        str_values = [str(v) if not isinstance(v, str) else v for v in values]
        return await self.client.rpush(key, *str_values)

    async def lpop(self, key: str) -> Optional[str]:
        """左弹出"""
        return await self.client.lpop(key)

    async def rpop(self, key: str) -> Optional[str]:
        """右弹出"""
        return await self.client.rpop(key)

    async def lrange(self, key: str, start: int = 0, end: int = -1) -> list:
        """获取列表范围"""
        return await self.client.lrange(key, start, end)

    # ============ 集合操作 ============

    async def sadd(self, key: str, *values: Any) -> int:
        """添加到集合"""
        return await self.client.sadd(key, *values)

    async def smembers(self, key: str) -> set:
        """获取集合所有成员"""
        return await self.client.smembers(key)

    async def sismember(self, key: str, value: Any) -> bool:
        """检查是否是集合成员"""
        return await self.client.sismember(key, value)

    # ============ 哈希操作 ============

    async def hset(self, key: str, field: str, value: Any) -> int:
        """设置哈希字段"""
        if not isinstance(value, str):
            value = json.dumps(value)
        return await self.client.hset(key, field, value)

    async def hget(self, key: str, field: str) -> Optional[str]:
        """获取哈希字段"""
        return await self.client.hget(key, field)

    async def hgetall(self, key: str) -> dict:
        """获取所有哈希字段"""
        return await self.client.hgetall(key)

    # ============ 分布式锁 ============

    async def lock(
        self,
        key: str,
        timeout: int = 10,
        blocking_timeout: int = 3
    ) -> Optional["redis.asyncio.Lock"]:
        """获取分布式锁"""
        lock = self.client.lock(key, timeout=timeout)
        if await lock.acquire(blocking_timeout=blocking_timeout):
            return lock
        return None

    # ============ 消息队列 ============

    async def enqueue(self, queue_name: str, task_data: dict) -> int:
        """入队"""
        task_json = json.dumps(task_data)
        return await self.rpush(f"queue:{queue_name}", task_json)

    async def dequeue(self, queue_name: str, timeout: int = 0) -> Optional[dict]:
        """出队（阻塞）"""
        if timeout > 0:
            result = await self.client.blpop(f"queue:{queue_name}", timeout=timeout)
            if result:
                _, value = result
                return json.loads(value)
        else:
            value = await self.lpop(f"queue:{queue_name}")
            if value:
                return json.loads(value)
        return None

    # ============ 缓存装饰器 ============

    def cached(self, key_prefix: str, ttl: int = 300):
        """缓存装饰器"""
        def decorator(func):
            async def wrapper(*args, **kwargs):
                cache_key = f"{key_prefix}:{':'.join(str(a) for a in args)}"
                cached_value = await self.get(cache_key)
                if cached_value:
                    return json.loads(cached_value)

                result = await func(*args, **kwargs)
                if result:
                    await self.setex(cache_key, ttl, result)
                return result
            return wrapper
        return decorator
