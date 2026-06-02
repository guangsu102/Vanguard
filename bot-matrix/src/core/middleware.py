"""风控中间件"""
from datetime import datetime
from typing import Optional

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
from aiogram.dispatcher.flags import get_flag

from loguru import logger


class RiskControlMiddleware(BaseMiddleware):
    """风控中间件 - 防薅羊毛"""

    def __init__(
        self,
        redis,
        max_per_ip_per_hour: int = 10,
        max_per_uid_per_day: int = 1
    ):
        self.redis = redis
        self.max_per_ip_per_hour = max_per_ip_per_hour
        self.max_per_uid_per_day = max_per_uid_per_day

    async def __call__(self, handler, event: TelegramObject, data: dict):
        """中间件调用"""
        if isinstance(event, Message):
            message = event
            user_id = message.from_user.id
            ip = self._get_client_ip(message)

            # 检查 IP 频率限制
            if ip:
                ip_key = f"risk:ip:{ip}:{datetime.now().strftime('%Y%m%d%H')}"
                ip_count = await self.redis.client.incr(ip_key)

                if ip_count == 1:
                    await self.redis.client.expire(ip_key, 3600)

                if ip_count > self.max_per_ip_per_hour:
                    logger.warning(f"IP {ip} 请求频率超限: {ip_count}")
                    await message.answer(
                        "请求过于频繁，请稍后再试。\n"
                        "如有问题请联系客服。"
                    )
                    return

            # 检查用户每日领取次数
            uid_key = f"risk:uid:{user_id}:{datetime.now().strftime('%Y%m%d')}"
            if await self.redis.client.exists(uid_key):
                uid_count = await self.redis.client.get(uid_key)
                if uid_count and int(uid_count) >= self.max_per_uid_per_day:
                    logger.warning(f"用户 {user_id} 今日已领取过试用账号")
                    await message.answer(
                        "您今日已领取过试用账号。\n"
                        "每人每天限领一次，明天再来有惊喜哦~"
                    )
                    return

        return await handler(event, data)

    def _get_client_ip(self, message: Message) -> Optional[str]:
        """获取客户端 IP（如果有）"""
        # Telegram 消息不直接包含 IP，此处预留接口
        # 实际使用时可通过其他方式获取
        return None


class AntiSpamMiddleware(BaseMiddleware):
    """反垃圾消息中间件"""

    def __init__(self, redis, blocked_words: list = None):
        self.redis = redis
        self.blocked_words = blocked_words or []

    async def __call__(self, handler, event: TelegramObject, data: dict):
        """中间件调用"""
        if isinstance(event, Message):
            message = event

            # 检查消息是否包含屏蔽词
            text = message.text or message.caption or ""
            for word in self.blocked_words:
                if word.lower() in text.lower():
                    logger.info(f"检测到屏蔽词 '{word}' in message from {message.from_user.id}")
                    await message.delete()
                    return

        return await handler(event, data)


class RateLimitMiddleware(BaseMiddleware):
    """通用限流中间件"""

    def __init__(
        self,
        redis,
        key_prefix: str,
        max_requests: int,
        window_seconds: int
    ):
        self.redis = redis
        self.key_prefix = key_prefix
        self.max_requests = max_requests
        self.window_seconds = window_seconds

    async def __call__(self, handler, event: TelegramObject, data: dict):
        """中间件调用"""
        if isinstance(event, Message):
            message = event
            key = f"ratelimit:{self.key_prefix}:{message.from_user.id}"

            current = await self.redis.client.incr(key)
            if current == 1:
                await self.redis.client.expire(key, self.window_seconds)

            if current > self.max_requests:
                ttl = await self.redis.client.ttl(key)
                await message.answer(
                    f"操作过于频繁，请在 {ttl} 秒后重试。"
                )
                return

        return await handler(event, data)
