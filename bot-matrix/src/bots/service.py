"""模块 B: 核心运营 Bot (Enhanced Service Bot)

使用 Telegram 用户账号矩阵

职责：
- 每日签到促活
- 弃单自动挽回
- 一键裂变海报生成
"""
import random
from typing import Dict

from telethon import TelegramClient, events
from loguru import logger

from ..core.database import Database
from ..core.cache import RedisClient
from ..core.api import XBoardAPIClient
from ..utils.poster import PosterGenerator
from ..utils.content import ContentTemplates


class ServiceHandler:
    """运营处理器"""

    def __init__(
        self,
        client: TelegramClient,
        db: Database,
        redis: RedisClient,
        api: XBoardAPIClient,
        poster: PosterGenerator,
        content: ContentTemplates,
        config: dict,
        account_name: str
    ):
        self.client = client
        self.db = db
        self.redis = redis
        self.api = api
        self.poster = poster
        self.content = content
        self.config = config
        self.account_name = account_name

        self._setup_handlers()

    def _setup_handlers(self):
        """设置消息处理器"""
        @self.client.on(events.NewMessage(incoming=True, pattern=r'^/checkin$'))
        async def handle_checkin(event):
            await self.process_checkin(event)

        @self.client.on(events.NewMessage(incoming=True, pattern=r'^/checkin_status'))
        async def handle_checkin_status(event):
            await self.process_checkin_status(event)

        @self.client.on(events.NewMessage(incoming=True, pattern=r'^/aff$'))
        async def handle_aff(event):
            await self.process_aff(event)

        @self.client.on(events.NewMessage(incoming=True, pattern=r'^/bind'))
        async def handle_bind(event):
            await self.process_bind(event)

        @self.client.on(events.NewMessage(incoming=True, pattern=r'^/myinfo'))
        async def handle_myinfo(event):
            await self.process_myinfo(event)

        @self.client.on(events.NewMessage(incoming=True, pattern=r'^/help'))
        async def handle_help(event):
            await self.process_help(event)

    async def process_checkin(self, event):
        """处理每日签到"""
        user_id = event.sender_id
        chat = await event.get_chat()
        first_name = getattr(chat, 'first_name', "") or "用户"

        checkin_config = self.config["service"]["checkin"]

        # 检查冷却
        cooldown_key = f"checkin:cooldown:{user_id}"
        if await self.redis.client.exists(cooldown_key):
            ttl = await self.redis.client.ttl(cooldown_key)
            hours = ttl // 3600
            minutes = (ttl % 3600) // 60
            remaining = f"{hours}小时{minutes}分钟" if hours > 0 else f"{minutes}分钟"
            await event.respond(f"{first_name}，签到太频繁啦~\n请 {remaining} 后再试。")
            return

        # 生成随机流量
        min_mb = checkin_config["min_traffic_mb"]
        max_mb = checkin_config["max_traffic_mb"]
        bonus_mb = random.randint(min_mb, max_mb)

        # 特殊奖励
        is_special = random.random() < 0.1
        if is_special:
            bonus_mb *= 2

        try:
            response = await self.api.add_traffic(tg_uid=user_id, traffic_mb=bonus_mb)

            if response["success"]:
                # 设置冷却
                cooldown_seconds = checkin_config["cooldown_hours"] * 3600
                await self.redis.client.setex(cooldown_key, cooldown_seconds, "1")

                # 记录签到
                await self.db.record_checkin(user_id=user_id, bonus_mb=bonus_mb, is_special=is_special)

                # 发送消息
                message = self.content.get_checkin_success(name=first_name, bonus_mb=bonus_mb, is_special=is_special)
                await event.respond(message)

                logger.info(f"[{self.account_name}] 用户 {user_id} 签到成功，获得 {bonus_mb}MB")

        except Exception as e:
            logger.error(f"签到处理失败: {e}")
            await event.respond("系统繁忙，请稍后再试。")

    async def process_checkin_status(self, event):
        """查看签到状态"""
        user_id = event.sender_id

        try:
            user_info = await self.api.get_user_info(tg_uid=user_id)

            if user_info["success"]:
                data = user_info["data"]
                remaining = data.get("traffic_remaining_gb", 0)
                expiry = data.get("expiry_date", "未知")

                cooldown_key = f"checkin:cooldown:{user_id}"
                can_checkin = not await self.redis.client.exists(cooldown_key)

                status = "可以签到" if can_checkin else "冷却中"

                await event.respond(
                    f"📊 账号状态\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"剩余流量：{remaining} GB\n"
                    f"到期时间：{expiry}\n"
                    f"签到状态：{status}\n"
                    f"━━━━━━━━━━━━━━━━━━"
                )

        except Exception as e:
            logger.error(f"获取签到状态失败: {e}")

    async def process_aff(self, event):
        """处理 /aff - 生成推广海报"""
        user_id = event.sender_id

        await event.respond("🎁 正在为您生成专属推广海报...\n请稍候...")

        try:
            aff_link_resp = await self.api.get_affiliate_link(tg_uid=user_id)

            if not aff_link_resp.get("success"):
                await event.respond("您还没有推广资格，请联系客服申请。")
                return

            aff_link = aff_link_resp["data"]["aff_link"]
            username = getattr(await event.get_chat(), 'username', None)

            # 生成海报
            poster_path = await self.poster.generate_simple_poster(
                user_id=user_id,
                aff_link=aff_link,
                username=username
            )

            # 发送海报
            await self.client.send_file(
                entity=event.sender_id,
                file=poster_path,
                caption=(
                    "您的专属推广海报已生成！\n\n"
                    "📱 保存图片分享给好友\n"
                    "🎯 每成功邀请 1 位付费用户，最高可获 50% 返佣\n\n"
                    f"推广链接：{aff_link}"
                )
            )

            logger.info(f"[{self.account_name}] 用户 {user_id} 生成推广海报成功")

        except Exception as e:
            logger.error(f"生成推广海报失败: {e}")
            await event.respond("海报生成失败，请稍后再试。")

    async def process_bind(self, event):
        """处理账号绑定"""
        user_id = event.sender_id
        args = event.text.split()[1:] if len(event.text.split()) > 1 else []

        if not args:
            await event.respond(
                "请提供您的 XBoard 账号密钥。\n\n"
                "绑定方式：\n"
                "/bind <您的账号密钥>\n\n"
                "获取密钥：登录 XBoard 控制台 → 个人设置 → API 密钥"
            )
            return

        api_key = args[0]

        try:
            response = await self.api.bind_telegram(tg_uid=user_id, api_key=api_key)

            if response["success"]:
                await event.respond(
                    "✅ 账号绑定成功！\n\n"
                    "现在您可以使用以下功能：\n"
                    "• /checkin - 每日签到领流量\n"
                    "• /aff - 生成推广海报\n"
                    "• /myinfo - 查看账号信息"
                )
            else:
                await event.respond(f"绑定失败：{response.get('message', '密钥无效')}")

        except Exception as e:
            logger.error(f"账号绑定失败: {e}")
            await event.respond("绑定失败，请稍后再试。")

    async def process_myinfo(self, event):
        """查看账号信息"""
        user_id = event.sender_id
        chat = await event.get_chat()

        try:
            user_info = await self.api.get_user_info(tg_uid=user_id)

            if user_info["success"]:
                data = user_info["data"]
                await event.respond(
                    "📋 账号信息\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"TG ID：{user_id}\n"
                    f"用户名：@{getattr(chat, 'username', '未设置') or '未设置'}\n"
                    f"剩余流量：{data.get('traffic_remaining_gb', 0)} GB\n"
                    f"到期时间：{data.get('expiry_date', '未知')}\n"
                    f"套餐：{data.get('plan_name', '试用版')}\n"
                    f"━━━━━━━━━━━━━━━━━━"
                )
            else:
                await event.respond("获取账号信息失败，请先绑定 /bind")

        except Exception as e:
            logger.error(f"获取账号信息失败: {e}")
            await event.respond("系统繁忙，请稍后再试。")

    async def process_help(self, event):
        """帮助命令"""
        await event.respond(
            "🔧 XBoard 服务助手\n\n"
            "可用命令：\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "/checkin - 每日签到（最高 1GB 流量）\n"
            "/checkin_status - 查看签到状态\n"
            "/aff - 生成专属推广海报\n"
            "/bind <密钥> - 绑定 XBoard 账号\n"
            "/myinfo - 查看账号信息\n"
            "/help - 显示帮助\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "遇到问题？请联系客服。"
        )


class ServiceBot:
    """核心运营 Bot - 多账号矩阵"""

    def __init__(
        self,
        account_manager,
        db: Database,
        redis: RedisClient,
        api: XBoardAPIClient,
        poster: PosterGenerator,
        config: dict
    ):
        self.account_manager = account_manager
        self.db = db
        self.redis = redis
        self.api = api
        self.poster = poster
        self.content = ContentTemplates()
        self.config = config
        self.handlers: Dict[str, ServiceHandler] = {}

        # 默认 handler（用于 handle_checkin）
        self._default_handler = None

        # 用于测试：直接传入依赖
        self._test_db = db
        self._test_redis = redis
        self._test_api = api

    async def start(self):
        """启动所有运营账号"""
        logger.info("启动核心运营 Bot 矩阵...")

        accounts = self.account_manager.get_enabled_accounts("service")

        for account in accounts:
            client = await self.account_manager.get_client(account.session_name)
            if client:
                handler = ServiceHandler(
                    client=client,
                    db=self.db,
                    redis=self.redis,
                    api=self.api,
                    poster=self.poster,
                    content=self.content,
                    config=self.config,
                    account_name=account.session_name
                )
                self.handlers[account.session_name] = handler
                logger.info(f"运营账号已启动: {account.name}")

        if not self.handlers:
            logger.warning("没有可用的运营账号")
            return

        logger.info(f"运营 Bot 矩阵启动完成，共 {len(self.handlers)} 个账号")

        # 设置默认 handler（使用第一个账号）
        if self.handlers:
            first_key = next(iter(self.handlers))
            self._default_handler = self.handlers[first_key]

    async def handle_checkin(self, message, callback):
        """处理签到请求（兼容测试接口）

        Args:
            message: 模拟的 Telegram Message 对象
            callback: 回调函数
        """
        # 如果有默认 handler，使用它
        if self._default_handler:
            await self._default_handler.process_checkin(message)
            return

        # 独立处理（用于测试）
        user_id = message.from_user.id
        first_name = message.from_user.first_name or "用户"

        checkin_config = self.config["service"]["checkin"]

        # 检查冷却
        cooldown_key = f"checkin:cooldown:{user_id}"
        if await self._test_redis.client.exists(cooldown_key):
            ttl = await self._test_redis.client.ttl(cooldown_key)
            hours = ttl // 3600
            minutes = (ttl % 3600) // 60
            remaining = f"{hours}小时{minutes}分钟" if hours > 0 else f"{minutes}分钟"
            await message.answer(f"{first_name}，签到太频繁啦~\n请 {remaining} 后再试。")
            return

        # 生成随机流量
        min_mb = checkin_config["min_traffic_mb"]
        max_mb = checkin_config["max_traffic_mb"]
        bonus_mb = random.randint(min_mb, max_mb)

        # 特殊奖励
        is_special = random.random() < 0.1
        if is_special:
            bonus_mb *= 2

        try:
            response = await self._test_api.add_traffic(tg_uid=user_id, traffic_mb=bonus_mb)

            if response["success"]:
                # 设置冷却
                cooldown_seconds = checkin_config["cooldown_hours"] * 3600
                await self._test_redis.client.setex(cooldown_key, cooldown_seconds, "1")

                # 记录签到
                await self._test_db.record_checkin(user_id=user_id, bonus_mb=bonus_mb, is_special=is_special)

                # 发送消息
                message_text = self.content.get_checkin_success(name=first_name, bonus_mb=bonus_mb, is_special=is_special)
                await message.answer(message_text)

        except Exception:
            await message.answer("系统繁忙，请稍后再试。")

    async def process_abandoned_orders(self):
        """处理弃单挽回"""
        cart_config = self.config["service"]["abandoned_cart"]
        if not cart_config["enabled"]:
            return

        try:
            orders = await self.api.get_unpaid_orders(
                timeout_minutes=cart_config["unpaid_timeout_minutes"]
            )

            for order in orders.get("data", []):
                user_id = order["tg_uid"]
                order_id = order["id"]

                reminder_key = f"cart_reminder:{order_id}"
                if await self.redis.client.exists(reminder_key):
                    continue

                # 生成优惠券
                discount = cart_config["discount_percent"]
                coupon = await self.api.create_coupon(
                    order_id=order_id,
                    discount_percent=discount,
                    validity_hours=cart_config["coupon_validity_hours"]
                )

                if coupon.get("success"):
                    coupon_data = coupon["data"]
                    message = self.content.get_abandoned_cart_message(
                        order_id=order_id,
                        amount=order["amount"],
                        coupon_code=coupon_data["code"],
                        discount=discount,
                        validity_hours=cart_config["coupon_validity_hours"]
                    )

                    # 向所有运营账号发送消息
                    for handler in self.handlers.values():
                        await handler.client.send_message(user_id, message)

                    await self.redis.client.setex(reminder_key, 86400, "1")

        except Exception as e:
            logger.error(f"处理弃单挽回失败: {e}")

    async def stop(self):
        """停止所有账号"""
        logger.info("停止核心运营 Bot 矩阵...")
        self.handlers.clear()
