"""模块 C: 社群风纪 Bot (Group Ops Bot)

使用 Telegram 用户账号矩阵

职责：
- 竞品与广告清洗
- 节点状态播报
"""
import re
from datetime import datetime
from typing import Dict, Optional

from telethon import TelegramClient, events
from loguru import logger

from ..core.database import Database
from ..core.cache import RedisClient
from ..core.api import XBoardAPIClient
from ..utils.content import ContentTemplates


class GroupOpsHandler:
    """社群风纪处理器"""

    def __init__(
        self,
        client: TelegramClient,
        db: Database,
        redis: RedisClient,
        api: XBoardAPIClient,
        config: dict,
        account_name: str
    ):
        self.client = client
        self.db = db
        self.redis = redis
        self.api = api
        self.config = config
        self.account_name = account_name
        self.content = ContentTemplates()

        # 竞品关键词正则
        self.keywords = config["group_ops"]["competitor_keywords"]
        self.keyword_patterns = [re.compile(kw, re.IGNORECASE) for kw in self.keywords]

        # 警告策略
        self.max_warnings = config["group_ops"]["warning"]["max_warnings"]
        self.ban_duration_hours = config["group_ops"]["warning"]["ban_duration_hours"]

        self._setup_handlers()

    def _setup_handlers(self):
        """设置消息处理器"""
        @self.client.on(events.NewMessage(chats=[], pattern=r'^/report'))
        async def handle_report(event):
            await self.process_report(event)

        @self.client.on(events.NewMessage(chats=[], pattern=r'^/status'))
        async def handle_status(event):
            await self.process_status(event)

    def check_competitor_content(self, text: str) -> Optional[str]:
        """检查是否包含竞品关键词"""
        if not text:
            return None

        for pattern in self.keyword_patterns:
            match = pattern.search(text)
            if match:
                return match.group()

        return None

    async def handle_violation(self, event, keyword: str):
        """处理违规消息"""
        user_id = event.sender_id
        chat_id = event.chat_id
        chat = await event.get_chat()
        first_name = getattr(chat, 'first_name', "") or ""

        logger.info(f"[{self.account_name}] 检测到违规内容，用户 {user_id}: {keyword}")

        try:
            # 删除消息
            await event.delete()
            logger.info(f"已删除用户 {user_id} 的违规消息")
        except Exception as e:
            logger.error(f"删除消息失败: {e}")

        # 获取警告次数
        warning_key = f"violation:warn:{chat_id}:{user_id}"
        warning_count = await self.redis.client.incr(warning_key)

        # 24小时过期
        await self.redis.client.expire(warning_key, 86400)

        if warning_count >= self.max_warnings:
            # 执行封禁
            try:
                await self.client.edit_permissions(
                    chat_id,
                    user_id,
                    view_messages=False
                )

                await self.client.send_message(
                    chat_id,
                    f"🚫 {first_name} 因多次违规已被永久移出群聊。\n\n违规内容：{keyword}"
                )

                await self.redis.client.delete(warning_key)

                # 记录封禁
                await self.db.record_ban(user_id=user_id, chat_id=chat_id, reason=keyword)

                logger.info(f"用户 {user_id} 因多次违规被封禁")

            except Exception as e:
                logger.error(f"封禁用户失败: {e}")
        else:
            remaining = self.max_warnings - warning_count

            await self.client.send_message(
                chat_id,
                f"⚠️ {first_name} 检测到违规内容：{keyword}\n\n"
                f"系统已记录违规 {warning_count}/{self.max_warnings} 次\n"
                f"剩余 {remaining} 次警告机会\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🚫 禁止发布竞品推广、非法链接\n"
                f"📢 有问题请联系管理员"
            )

    async def process_report(self, event):
        """处理举报命令"""
        args = event.text.split(maxsplit=1)

        if len(args) < 2:
            await event.respond(
                "📢 使用方式：/report <举报内容>\n\n"
                "示例：\n"
                "/report 发现有人发广告\n"
                "/report 节点无法连接"
            )
            return

        report_content = args[1]
        user_id = event.sender_id
        chat_id = event.chat_id

        # 转发给管理员
        admin_chat_id = self.config["monitoring"]["admin_chat_id"]
        if admin_chat_id:
            admin_message = (
                f"📢 群组举报\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"群组：{chat_id}\n"
                f"举报人：{user_id}\n"
                f"内容：{report_content}\n"
                f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )

            try:
                await self.client.send_message(admin_chat_id, admin_message)
                await event.respond("✅ 您的举报已提交，管理员会尽快处理。")
            except Exception as e:
                logger.error(f"转发举报失败: {e}")
                await event.respond("举报提交失败，请稍后重试。")

    async def process_status(self, event):
        """查看节点状态"""
        await event.respond("正在获取节点状态...")

        try:
            node_status = await self.api.get_node_status()

            if node_status["success"]:
                nodes = node_status["data"]
                total = len(nodes)
                online = sum(1 for n in nodes if n.get("status") == "online")
                offline = total - online

                status_text = (
                    f"📊 节点状态报告\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"总节点数：{total}\n"
                    f"在线：{online} ✅\n"
                    f"离线：{offline} ❌\n"
                    f"━━━━━━━━━━━━━━━━━━\n\n"
                )

                for node in nodes[:5]:
                    name = node.get("name", "未知")
                    status = "✅" if node.get("status") == "online" else "❌"
                    latency = node.get("latency_ms", "-")
                    status_text += f"{status} {name} | 延迟 {latency}ms\n"

                if total > 5:
                    status_text += f"\n... 还有 {total - 5} 个节点"

                await event.respond(status_text)
            else:
                await event.respond("获取节点状态失败")

        except Exception as e:
            logger.error(f"获取节点状态失败: {e}")
            await event.respond("获取节点状态失败，请稍后重试。")


class GroupOpsBot:
    """社群风纪 Bot - 多账号矩阵"""

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
        self.handlers: Dict[str, GroupOpsHandler] = {}

        # 监听群组列表
        self.monitored_groups: list = []

    async def start(self):
        """启动所有管理账号"""
        logger.info("启动社群风纪 Bot 矩阵...")

        accounts = self.account_manager.get_enabled_accounts("group_ops")

        for account in accounts:
            client = await self.account_manager.get_client(account.session_name)
            if client:
                handler = GroupOpsHandler(
                    client=client,
                    db=self.db,
                    redis=self.redis,
                    api=self.api,
                    config=self.config,
                    account_name=account.session_name
                )

                # 设置群组消息监听
                self._setup_group_handlers(handler)

                self.handlers[account.session_name] = handler
                logger.info(f"管理账号已启动: {account.name}")

        if not self.handlers:
            logger.warning("没有可用的管理账号")
            return

        logger.info(f"社群风纪 Bot 矩阵启动完成，共 {len(self.handlers)} 个账号")

    def _setup_group_handlers(self, handler: GroupOpsHandler):
        """设置群组处理器"""
        @handler.client.on(events.NewMessage(chats=self.monitored_groups))
        async def handle_group_message(event):
            # 检查竞品内容
            text = event.text or ""
            matched_keyword = handler.check_competitor_content(text)

            if matched_keyword:
                await handler.handle_violation(event, matched_keyword)

        @handler.client.on(events.ChatAction(chats=self.monitored_groups))
        async def handle_chat_action(event):
            # 新成员加入
            if event.user_joined or event.user_added:
                user_id = event.action_message.sender_id
                first_name = getattr(event.action_message.sender, 'first_name', "") or ""

                # 检查是否被封禁
                ban_key = f"violation:ban:{event.chat_id}:{user_id}"
                if await self.redis.client.exists(ban_key):
                    try:
                        await handler.client.edit_permissions(
                            event.chat_id,
                            user_id,
                            view_messages=False
                        )
                    except Exception as e:
                        logger.error(f"拒绝被封禁用户加入失败: {e}")
                    return

                await handler.client.send_message(
                    event.chat_id,
                    f"👋 欢迎 {first_name} 加入！\n"
                    f"请阅读群公告了解使用规则\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"💡 遇到问题？发送 /report 联系管理员"
                )

    async def add_monitored_group(self, chat_id: int):
        """添加监控群组"""
        if chat_id not in self.monitored_groups:
            self.monitored_groups.append(chat_id)
            logger.info(f"添加监控群组: {chat_id}")

            # 重新设置处理器
            for handler in self.handlers.values():
                self._setup_group_handlers(handler)

    async def send_node_report(self, chat_id: int):
        """发送节点状态播报"""
        try:
            node_status = await self.api.get_node_status()

            if node_status["success"]:
                nodes = node_status["data"]
                total = len(nodes)
                online = sum(1 for n in nodes if n.get("status") == "online")
                offline = total - online

                report = (
                    f"🌙 晚高峰节点状态播报\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"总节点数：{total}\n"
                    f"在线：{online} ✅  离线：{offline} ❌\n"
                    f"━━━━━━━━━━━━━━━━━━\n\n"
                    f"各节点详情：\n"
                )

                for node in nodes:
                    name = node.get("name", "未知")
                    status = "🟢" if node.get("status") == "online" else "🔴"
                    latency = node.get("latency_ms", "-")
                    load = node.get("load_percent", "-")
                    report += f"{status} {name}\n   延迟: {latency}ms | 负载: {load}%\n\n"

                report += (
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"如遇问题，请联系管理员\n"
                    f"报告时间：{datetime.now().strftime('%H:%M')}"
                )

                # 向所有管理账号发送
                for handler in self.handlers.values():
                    await handler.client.send_message(chat_id, report)

                logger.info(f"节点状态播报已发送至群组 {chat_id}")

        except Exception as e:
            logger.error(f"发送节点状态播报失败: {e}")

    async def stop(self):
        """停止所有账号"""
        logger.info("停止社群风纪 Bot 矩阵...")
        self.handlers.clear()
