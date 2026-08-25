"""模块 A: 引流空投 Bot - 完整实现

支持：
- Telegram 用户账号矩阵
- 动态关键词管理
- 群组账号分配（每群最多3个账号）
- 群组分級策略
"""
import asyncio
import random
from datetime import datetime
from typing import Dict, List
from dataclasses import dataclass

from telethon import TelegramClient, events
from loguru import logger

from ..core.database import Database
from ..core.cache import RedisClient
from ..core.api import XBoardAPIClient
from .keywords import KeywordManager
from .group_manager import GroupManager, GroupGrade


@dataclass
class LeadGenConfig:
    """引流配置"""
    # 防封号配置
    message_interval: int = 30  # 发消息间隔（秒）
    max_messages_per_day: int = 30  # 每账号每天最大消息数
    max_groups_per_day: int = 10  # 每天最大加群数

    # 发言策略
    typing_delay: tuple = (2000, 8000)  # 打字延迟范围（毫秒）
    random_timing: bool = True

    # 融入策略
    min_messages_before_promo: int = 5  # 引流前最少发言数
    min_wait_hours: int = 24  # 加入后最少等待时间


class SafeMessages:
    """安全发言内容池"""

    # 简短互动
    SHORT_REACTIONS = [
        "👍", "👍👍", "666", "感谢", "厉害", "同问",
        "赞", "好", "收到", "明白", "支持", "学习",
        "有用", "👍🏻", "👍🏼", "👌"
    ]

    # 普通问题
    QUESTIONS = [
        "这个是什么节点？",
        "速度怎么样？",
        "支持iOS吗？",
        "稳定性如何？",
        "怎么选节点？",
        "在哪下载客户端？",
        "需要设置吗？",
        "怎么看速度？",
        "有教程吗？",
        "收费吗？",
    ]

    # 融入发言
    INTEGRATION = [
        "刚接触，还不太懂",
        "新人报道",
        "大佬们好",
        "求带",
        "萌新求指教",
        "跟着大家走",
        "终于找到了",
        "谢谢分享",
        "这个好用吗？",
        "多少钱一个月？",
    ]

    # 软性引流
    SOFT_PROMO = [
        "之前用的别家，感觉一般",
        "朋友推荐了一家，还不错",
        "有人用过这家吗？",
        "看群里推荐过，想试试",
        "哪家比较稳定啊？",
    ]

    @classmethod
    def get_random(cls, category: str = "any") -> str:
        """获取随机消息"""
        if category == "reaction":
            return random.choice(cls.SHORT_REACTIONS)
        elif category == "question":
            return random.choice(cls.QUESTIONS)
        elif category == "integration":
            return random.choice(cls.INTEGRATION)
        elif category == "soft_promo":
            return random.choice(cls.SOFT_PROMO)
        else:
            return random.choice(
                cls.SHORT_REACTIONS + cls.QUESTIONS + cls.INTEGRATION
            )


class LeadGenHandler:
    """引流处理器"""

    def __init__(
        self,
        client: TelegramClient,
        db: Database,
        redis: RedisClient,
        api: XBoardAPIClient,
        keyword_manager: KeywordManager,
        group_manager: GroupManager,
        config: LeadGenConfig,
        account_name: str,
        role: str = "main"  # main / aux1 / aux2
    ):
        self.client = client
        self.db = db
        self.redis = redis
        self.api = api
        self.keywords = keyword_manager
        self.groups = group_manager
        self.config = config
        self.account_name = account_name
        self.role = role

        # 统计
        self.stats = {
            "messages_sent": 0,
            "groups_joined": 0,
            "keywords_triggered": 0,
            "daily_reset": datetime.now()
        }

        self._setup_handlers()

    def _setup_handlers(self):
        """设置消息处理器"""
        @self.client.on(events.NewMessage(incoming=True))
        async def handle_incoming(event):
            await self.process_incoming(event)

        @self.client.on(events.ChatAction)
        async def handle_chat_action(event):
            await self.process_chat_action(event)

    async def process_incoming(self, event):
        """处理收到的消息"""
        chat = await event.get_chat()

        # 跳过群组消息（或者只处理关键词触发）
        if hasattr(chat, 'megagroup') and chat.megagroup:
            await self.handle_group_message(event, chat)
        elif hasattr(chat, 'first_name'):  # 私聊
            await self.handle_private_message(event, chat)

    async def handle_group_message(self, event, chat):
        """处理群组消息"""
        text = event.text or ""

        # 匹配关键词
        matches = self.keywords.match(text)

        # 检查排除关键词
        if matches["exclude"]:
            logger.info(f"检测到排除关键词: {matches['exclude']}")
            return

        # 触发关键词处理
        if matches["trigger"]:
            self.stats["keywords_triggered"] += 1
            await self.handle_trigger(event, matches["trigger"])

        # 目标群关键词 - 收集情报
        if matches["target"]:
            await self.collect_intel(event, matches["target"])

    async def handle_trigger(self, event, trigger_keywords: List[str]):
        """处理触发关键词"""
        sender = await event.get_sender()
        sender_id = sender.id

        # 检查是否已处理过
        processed_key = f"triggered:{sender_id}:{event.id}"
        if await self.redis.client.exists(processed_key):
            return

        # 根据角色决定行为
        if self.role == "main":
            # 主号：直接私信引导
            await self.send_private_guide(event, sender_id, trigger_keywords)
        elif self.role in ["aux1", "aux2"]:
            # 辅号：标记用户或简短回复
            await self.mark_potential_user(event, sender_id)

        # 标记已处理
        await self.redis.client.setex(processed_key, 86400, "1")

    async def send_private_guide(self, event, sender_id: int, keywords: List[str]):
        """发送私信引导"""
        try:
            guide_text = self._build_guide_message(keywords)
            await self.client.send_message(sender_id, guide_text)
            logger.info(f"向用户 {sender_id} 发送引导消息")
        except Exception as e:
            logger.error(f"发送引导消息失败: {e}")

    def _build_guide_message(self, keywords: List[str]) -> str:
        """构建引导消息"""
        # 根据关键词选择模板
        if any(k in ["哪里买", "怎么购买", "多少钱", "价格"] for k in keywords):
            return (
                "看到你在问购买问题 👋\n\n"
                "我们现在有优惠活动：\n"
                "• 新用户首月 5 折\n"
                "• 免费试用 24 小时\n\n"
                "点击领取：[链接]\n\n"
                "有问题随时问我~"
            )
        elif any(k in ["怎么用", "如何使用", "教程", "怎么连接"] for k in keywords):
            return (
                "看到你在问使用问题，我来帮你 👇\n\n"
                "1️⃣ 先下载客户端\n"
                "2️⃣ 导入订阅链接\n"
                "3️⃣ 选择节点连接\n\n"
                "需要订阅链接的话私信我~"
            )
        else:
            return (
                "你好！我这边有优质节点推荐 👋\n\n"
                "• 高速稳定\n"
                "• 支持全平台\n"
                "• 免费试用\n\n"
                "感兴趣的话私信我~"
            )

    async def mark_potential_user(self, event, sender_id: int):
        """标记潜在用户"""
        # 存储到 Redis，供后续处理
        key = f"potential_user:{sender_id}"
        await self.redis.client.lpush(key, datetime.now().isoformat())
        await self.redis.client.expire(key, 86400 * 7)

    async def collect_intel(self, event, target_keywords: List[str]):
        """收集情报"""
        # 记录竞品动态或市场信息
        intel_key = f"intel:{datetime.now().strftime('%Y%m%d')}"
        intel_data = {
            "keyword": target_keywords,
            "sender_id": event.sender_id,
            "chat_id": event.chat_id,
            "time": datetime.now().isoformat()
        }
        import json
        await self.redis.client.lpush(intel_key, json.dumps(intel_data))

    async def handle_private_message(self, event, chat):
        """处理私信"""
        text = event.text or ""

        if text.startswith("/start"):
            await self.handle_start(event)
        elif text.startswith("/help"):
            await self.handle_help(event)
        elif "试用" in text or "免费" in text:
            await self.handle_trial_request(event)

    async def handle_start(self, event):
        """处理 /start"""
        await event.respond(
            "欢迎！发送「试用」可领取免费体验账号~\n"
            "发送「帮助」查看更多功能。"
        )

    async def handle_help(self, event):
        """处理 /help"""
        await event.respond(
            "📖 帮助信息\n\n"
            "发送「试用」- 领取免费试用\n"
            "发送「价格」- 查看套餐价格\n"
            "发送「教程」- 查看使用教程"
        )

    async def handle_trial_request(self, event):
        """处理试用请求"""
        user_id = event.sender_id

        # 创建试用账号
        try:
            response = await self.api.create_trial_user(
                tg_uid=user_id,
                username=getattr(await event.get_chat(), 'username', '') or '',
                validity_hours=24,
                traffic_gb=50
            )

            if response["success"]:
                trial_info = response["data"]
                await event.respond(
                    f"🎁 恭喜！试用账号创建成功！\n\n"
                    f"• 有效期：24小时\n"
                    f"• 流量：50GB\n"
                    f"• 到期：{trial_info.get('expires_at', '24小时后')}\n\n"
                    f"订阅链接已发送到您的订阅客户端。"
                )
        except Exception as e:
            logger.error(f"创建试用账号失败: {e}")
            await event.respond("系统繁忙，请稍后再试。")

    async def process_chat_action(self, event):
        """处理群组动作"""
        if event.user_joined():
            # 新成员加入
            await self.handle_user_joined(event)
        elif event.user_kicked():
            # 被踢出
            await self.handle_kicked(event)

    async def handle_user_joined(self, event):
        """处理用户加入"""
        chat_id = event.chat_id

        # 获取我的账号
        me = await self.client.get_me()

        # 检查是否是bot加入
        if event.action_message.action.get('users', []):
            for uid in event.action_message.action['users']:
                if uid == me.id:
                    # 我加入了群组
                    await self.on_joined_group(chat_id)

    async def handle_kicked(self, event):
        """处理被踢"""
        chat_id = event.chat_id
        me = await self.client.get_me()

        if event.action_message.action.get('users', []):
            for uid in event.action_message.action['users']:
                if uid == me.id:
                    # 我被踢出
                    self.groups.mark_kicked(chat_id)
                    logger.warning(f"账号 {self.account_name} 被踢出群组 {chat_id}")

    async def on_joined_group(self, chat_id: int):
        """成功加入群组后的处理"""
        # 分配账号
        self.groups.assign_account(chat_id, self.account_name, self.role)

        # 检测群组类型
        await self.detect_group_type(chat_id)

        # 根据角色决定行为
        if self.role == "main":
            group = self.groups.get_group(chat_id)
            if group and group.grade == GroupGrade.GRADE_A:
                # A级群：直接发广告
                await self.send_group_promo(chat_id)
            elif group and group.grade == GroupGrade.GRADE_B:
                # B级群：开始融入
                await self.start_integration(chat_id)
            else:
                # C/D级群：静默监控
                pass

    async def detect_group_type(self, chat_id: int):
        """检测群组类型"""
        try:
            # 获取最近消息
            messages = []
            async for msg in self.client.iter_messages(chat_id, limit=20):
                messages.append(msg)

            if not messages:
                self.groups.set_grade(chat_id, GroupGrade.GRADE_C)
                return

            # 分析消息发送者
            admin_messages = 0
            total_messages = len(messages)

            for msg in messages:
                sender = await msg.get_sender()
                if sender:
                    # 简单判断：如果全是同一个人发，可能是仅管理员
                    if hasattr(msg, 'sender_id'):
                        if messages[0].sender_id == msg.sender_id:
                            admin_messages += 1

            # 计算管理员消息占比
            admin_ratio = admin_messages / total_messages if total_messages > 0 else 1

            if admin_ratio > 0.8:
                self.groups.set_grade(chat_id, GroupGrade.GRADE_C)
            elif admin_ratio > 0.5:
                self.groups.set_grade(chat_id, GroupGrade.GRADE_B)
            else:
                self.groups.set_grade(chat_id, GroupGrade.GRADE_B)

            logger.info(f"群组 {chat_id} 分类为 {self.groups.get_group(chat_id).grade.value} 级")

        except Exception as e:
            logger.error(f"检测群组类型失败: {e}")
            self.groups.set_grade(chat_id, GroupGrade.GRADE_C)

    async def send_group_promo(self, chat_id: int):
        """在群组发送推广"""
        try:
            # 模拟打字
            if self.config.random_timing:
                delay = random.randint(*self.config.typing_delay) / 1000
                await asyncio.sleep(delay)

            promo_text = (
                "🚀 推荐一个高速稳定的节点服务\n\n"
                "• 专线节点，速度快\n"
                "• 支持全平台客户端\n"
                "• 新用户免费试用\n\n"
                "有兴趣的私信我~"
            )

            await self.client.send_message(chat_id, promo_text)
            self.stats["messages_sent"] += 1

        except Exception as e:
            logger.error(f"发送群组推广失败: {e}")

    async def start_integration(self, chat_id: int):
        """开始融入群组"""
        # 记录开始融入时间
        key = f"integration_start:{chat_id}:{self.account_name}"
        await self.redis.client.set(key, datetime.now().isoformat())
        await self.redis.client.expire(key, 86400 * 7)

        # 发送欢迎消息
        await self.send_integration_message(chat_id)

    async def send_integration_message(self, chat_id: int):
        """发送融入消息"""
        try:
            message = SafeMessages.get_random("integration")

            if self.config.random_timing:
                delay = random.randint(*self.config.typing_delay) / 1000
                await asyncio.sleep(delay)

            await self.client.send_message(chat_id, message)
            self.stats["messages_sent"] += 1

        except Exception as e:
            logger.error(f"发送融入消息失败: {e}")


class LeadGenBot:
    """引流空投 Bot - 多账号矩阵"""

    def __init__(
        self,
        account_manager,
        db: Database,
        redis: RedisClient,
        api: XBoardAPIClient,
        config: dict
    ):
        self.account_manager = account_manager
        self.db = db
        self.redis = redis
        self.api = api
        self.config = config

        # 初始化管理组件
        self.keywords = KeywordManager(redis)
        self.groups = GroupManager(redis)

        # 处理器映射
        self.handlers: Dict[str, LeadGenHandler] = {}

        # BotMatrix passes the root config; anti-ban values live under lead_gen.
        anti_ban_config = config.get("lead_gen", {}).get("anti_ban", {})
        self.lead_config = LeadGenConfig(
            message_interval=anti_ban_config.get("message_interval", 30),
            max_messages_per_day=anti_ban_config.get("max_messages_per_day", 30),
            max_groups_per_day=anti_ban_config.get("max_groups_per_day", 10),
            typing_delay=tuple(anti_ban_config.get("typing_delay", [2000, 8000])),
            random_timing=anti_ban_config.get("random_timing", True),
            min_messages_before_promo=5,
            min_wait_hours=24,
        )

    async def start(self):
        """启动"""
        logger.info("启动引流空投 Bot...")

        # 初始化关键词和群组
        await self.keywords.initialize()
        await self.groups.initialize()

        # 启动各账号
        accounts = self.account_manager.get_enabled_accounts("lead_gen")

        for account in accounts:
            client = await self.account_manager.get_client(account.session_name)
            if client:
                # 确定角色
                role = self._assign_role(account.session_name)

                handler = LeadGenHandler(
                    client=client,
                    db=self.db,
                    redis=self.redis,
                    api=self.api,
                    keyword_manager=self.keywords,
                    group_manager=self.groups,
                    config=self.lead_config,
                    account_name=account.session_name,
                    role=role
                )
                self.handlers[account.session_name] = handler

        logger.info(f"引流 Bot 启动完成，{len(self.handlers)} 个账号")

    def _assign_role(self, account_name: str) -> str:
        """分配角色"""
        # 简单的角色分配策略
        # 实际应该根据账号状态动态调整
        if "main" in account_name or "1" in account_name:
            return "main"
        elif "aux" in account_name or "sub" in account_name or "2" in account_name:
            return "aux1"
        else:
            return "aux2"

    async def add_keyword(self, keyword_type: str, keyword: str) -> bool:
        """添加关键词"""
        return self.keywords.add(keyword_type, keyword)

    async def remove_keyword(self, keyword_type: str, keyword: str) -> bool:
        """删除关键词"""
        return self.keywords.remove(keyword_type, keyword)

    async def get_keywords(self, keyword_type: str = None) -> List[str]:
        """获取关键词"""
        return self.keywords.list_keywords(keyword_type)

    async def check_subscription(self, user_id: int) -> tuple:
        """检查用户订阅状态

        Args:
            user_id: Telegram 用户 ID

        Returns:
            (是否全部订阅, 未订阅的频道列表)
        """
        not_subscribed = []
        official_channels = self.config.get("telegram", {}).get("official_channels", [])

        for channel_username in official_channels:
            try:
                # 通过 bot 检查用户是否在频道中
                member = await self.bot.get_chat_member(channel_username, user_id)
                # 检查状态：member、creator、administrator 都是已订阅
                if member.status not in ("member", "creator", "administrator"):
                    not_subscribed.append(channel_username)
            except Exception as e:
                logger.warning(f"检查频道 {channel_username} 订阅状态失败: {e}")
                not_subscribed.append(channel_username)

        is_subscribed = len(not_subscribed) == 0
        return is_subscribed, not_subscribed

    async def stop(self):
        """停止"""
        self.handlers.clear()
        logger.info("引流 Bot 已停止")
