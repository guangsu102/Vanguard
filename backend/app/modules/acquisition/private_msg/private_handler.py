"""
Private Handler Module

Handles private messages and user conversations.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlparse, parse_qs

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.pool import AccountPool
from app.core.redis import RateLimiter, RedisCache
from app.modules.acquisition.private_msg.dialog_manager import DialogManager
from app.modules.acquisition.private_msg.welcome import WelcomeGenerator
from app.modules.acquisition.private_msg.guide_flow import GuideFlowManager
from app.modules.acquisition.tracking.tracker import Tracker
from app.modules.acquisition.config import AcquisitionConfig
from app.modules.acquisition.exceptions import MessageSendError

logger = structlog.get_logger()


@dataclass
class PrivateMessageResult:
    """Result of private message handling."""
    success: bool
    reply: Optional[str] = None
    error: Optional[str] = None
    action_taken: Optional[str] = None


class PrivateHandler:
    """
    Private message handler for user conversations.

    Manages private message processing, welcome messages,
    guide flows, and conversation tracking.
    """

    def __init__(
        self,
        db: AsyncSession,
        account_pool: AccountPool,
        dialog_manager: DialogManager,
        guide_flow_manager: GuideFlowManager,
        tracker: Tracker,
        config: Optional[AcquisitionConfig] = None,
    ):
        """
        Initialize PrivateHandler.

        Args:
            db: Database session
            account_pool: Account pool for sending messages
            dialog_manager: Dialog manager for conversation state
            guide_flow_manager: Guide flow manager
            tracker: Tracking module
            config: Optional configuration
        """
        self.db = db
        self.account_pool = account_pool
        self.dialog_manager = dialog_manager
        self.guide_flow_manager = guide_flow_manager
        self.tracker = tracker
        self.config = config or AcquisitionConfig()
        self.welcome_generator = WelcomeGenerator()
        self.logger = logger.bind(module="private_handler")
        self._send_lock = asyncio.Lock()

    async def handle_message(
        self,
        user_id: int,
        message_text: str,
        source: str = "unknown",
    ) -> PrivateMessageResult:
        """
        Handle an incoming private message.

        Args:
            user_id: User ID who sent the message
            message_text: Message text
            source: Source of the message (group/private/command)

        Returns:
            PrivateMessageResult with handling result
        """
        async with self._send_lock:
            self.logger.info(
                "handle_private_message",
                user_id=user_id,
                source=source,
                message_length=len(message_text),
            )

            try:
                # 处理命令
                if message_text.startswith("/"):
                    return await self._handle_command(user_id, message_text)

                # 获取对话上下文
                context = await self.dialog_manager.get_or_create_context(user_id)

                # 处理引导流程
                if context.state.value in ["init", "awaiting_registration"]:
                    guide_result = await self.guide_flow_manager.handle_step(
                        user_id=user_id,
                        user_message=message_text,
                        context=context,
                    )
                    if guide_result.get("completed"):
                        return PrivateMessageResult(
                            success=True,
                            reply=guide_result.get("reply"),
                            action_taken="guide_flow",
                        )

                # 处理普通对话
                reply = await self._generate_reply(user_id, message_text, context)
                if reply:
                    await self.send_message(user_id, reply)

                return PrivateMessageResult(success=True, reply=reply)

            except Exception as e:
                self.logger.error("handle_message_error", user_id=user_id, error=str(e))
                return PrivateMessageResult(success=False, error=str(e))

    async def _handle_command(
        self,
        user_id: int,
        command: str,
    ) -> PrivateMessageResult:
        """Handle a command message."""
        command = command.strip().lower()

        if command.startswith("/start"):
            return await self._handle_start(user_id, command)
        elif command == "/help":
            return await self._handle_help(user_id)
        elif command == "/register":
            return await self._handle_register(user_id)
        elif command == "/status":
            return await self._handle_status(user_id)
        else:
            return PrivateMessageResult(
                success=True,
                reply="未知命令，请发送 /help 查看可用命令",
            )

    async def _handle_start(self, user_id: int, command: str = "/start") -> PrivateMessageResult:
        """Handle /start command."""
        # 解析追踪码
        tracking_code = self._extract_tracking_code(command)
        if tracking_code:
            await self.tracker.record_tracking_code(user_id=user_id, tracking_code=tracking_code)

        # 发送欢迎消息
        welcome = await self.welcome_generator.generate_welcome(user_id)
        success = await self.send_message(user_id, welcome)

        # 创建引导流程
        if success:
            await self.guide_flow_manager.start_flow(user_id)

        return PrivateMessageResult(
            success=success,
            reply=welcome,
            action_taken="welcome",
        )

    async def _handle_help(self, user_id: int) -> PrivateMessageResult:
        """Handle /help command."""
        help_text = """
可用命令：
/start - 开始使用
/help - 显示此帮助
/register - 获取注册链接
/status - 查看状态

有其他问题欢迎随时咨询！
"""
        success = await self.send_message(user_id, help_text.strip())
        return PrivateMessageResult(success=success, reply=help_text, action_taken="help")

    async def _handle_register(self, user_id: int) -> PrivateMessageResult:
        """Handle /register command."""
        # 获取追踪链接
        tracking_link = await self.tracker.generate_tracking_link(user_id)
        reply = f"点击下面的链接注册：\n{tracking_link}\n\n注册成功后会有专属客服为您服务~"
        success = await self.send_message(user_id, reply)
        return PrivateMessageResult(success=success, reply=reply, action_taken="register_link")

    async def _handle_status(self, user_id: int) -> PrivateMessageResult:
        """Handle /status command."""
        status = await self._get_xboard_status(user_id)
        if status:
            reply = f"您的当前状态：{status}"
        else:
            reply = "正在查询您的状态，请稍候..."
        success = await self.send_message(user_id, reply)
        return PrivateMessageResult(success=success, reply=reply, action_taken="status_check")

    async def _generate_reply(
        self,
        user_id: int,
        message_text: str,
        context,
    ) -> Optional[str]:
        """Generate a reply to user message."""
        text_lower = message_text.lower().strip()

        # 检查意图
        if any(kw in text_lower for kw in ["谢谢", "好的", "收到"]):
            return "不客气！有需要随时找我~"
        elif any(kw in text_lower for kw in ["怎么用", "如何使用", "帮助"]):
            return "可以发送 /help 查看帮助，或者直接告诉我您想了解什么~"
        elif any(kw in text_lower for kw in ["注册", "试用", "体验"]):
            return await self._get_register_prompt(user_id)
        elif any(kw in text_lower for kw in ["价格", "多少钱", "收费"]):
            return "XBoard 提供多种套餐，价格实惠。新用户有优惠活动，点击注册了解："
        elif any(kw in text_lower for kw in ["节点", "速度", "稳定"]):
            return "XBoard 节点覆盖全球，速度稳定。支持多设备同时使用，欢迎体验~"

        # 默认回复
        return "收到您的消息了！有什么可以帮助您的吗？发送 /help 查看可用命令~"

    async def _get_register_prompt(self, user_id: int) -> str:
        """Get registration prompt with tracking link."""
        tracking_link = await self.tracker.generate_tracking_link(user_id)
        return f"点击下方链接注册体验：\n{tracking_link}\n\n新用户有免费试用机会哦~"

    async def send_message(
        self,
        user_id: int,
        message: str,
        account_id: Optional[int] = None,
    ) -> bool:
        """
        Send a private message to a user.

        Args:
            user_id: Target user ID
            message: Message content
            account_id: Optional specific account to use

        Returns:
            True if successful
        """
        self.logger.info("send_private_message", user_id=user_id, message_length=len(message))

        # 获取账号
        account = None
        if account_id:
            account = await self.account_pool.get_account_by_id(account_id)
        if not account:
            try:
                account = await self.account_pool.acquire(purpose="private")
            except Exception as e:
                self.logger.error("no_available_account", error=str(e))
                return False

        if account is None:
            self.logger.warning("no_account_available", user_id=user_id)
            return False

        try:
            # 检查间隔
            if not await self._check_send_interval(user_id):
                self.logger.warning("send_interval_violated", user_id=user_id)
                return False

            client = getattr(account, "client", None)
            if client is None:
                client = await self._ensure_client(account)
            if client is not None:
                result = await client.send_message(user_id, message)
                msg_id = getattr(result, "id", getattr(result, "message_id", None))
            else:
                self.logger.warning("private_send_no_client", user_id=user_id)
                msg_id = None

            self.logger.info("message_sent", user_id=user_id, message_id=msg_id)
            return True

        except Exception as e:
            self.logger.error("send_message_failed", user_id=user_id, error=str(e))
            return False

        finally:
            if account:
                await self.account_pool.release(account)

    async def send_welcome(
        self,
        user_id: int,
        source_info: Optional[dict] = None,
    ) -> bool:
        """
        Send welcome message to a new user.

        Args:
            user_id: Target user ID
            source_info: Optional source information

        Returns:
            True if successful
        """
        welcome = await self.welcome_generator.generate_welcome(user_id, source_info)
        return await self.send_message(user_id, welcome)

    async def send_guide_message(
        self,
        user_id: int,
        step: str,
        **kwargs,
    ) -> bool:
        """
        Send guide message for a specific step.

        Args:
            user_id: Target user ID
            step: Guide step identifier
            **kwargs: Additional template variables

        Returns:
            True if successful
        """
        message = await self.guide_flow_manager.get_step_message(step, **kwargs)
        if message:
            return await self.send_message(user_id, message)
        return False

    async def send_registration_link(
        self,
        user_id: int,
        tracking_code: Optional[str] = None,
    ) -> bool:
        """
        Send registration link to user.

        Args:
            user_id: Target user ID
            tracking_code: Optional tracking code

        Returns:
            True if successful
        """
        if not tracking_code:
            tracking_code = str(user_id)

        link = await self.tracker.generate_tracking_link(user_id, tracking_code=tracking_code)
        message = f"点击下方链接注册：\n{link}\n\n注册成功后即可享受新用户优惠~"
        return await self.send_message(user_id, message)

    async def generate_invite_message(
        self,
        user_id: int,
        source_group_id: Optional[int] = None,
        keyword: Optional[str] = None,
    ) -> str:
        """
        Generate invite message for user.

        Args:
            user_id: Target user ID
            source_group_id: Source group ID
            keyword: Trigger keyword

        Returns:
            Generated message text
        """
        tracking_link = await self.tracker.generate_tracking_link(
            user_id,
            group_id=source_group_id,
            keyword=keyword,
        )

        message = f"您好！感谢您的兴趣~\n\n点击链接注册体验：\n{tracking_link}\n\n新用户有免费试用机会，期待您的加入！"

        return message

    async def _ensure_client(self, account) -> Optional[object]:
        """Ensure an account has a connected Telegram client."""
        client = getattr(account, "client", None)
        if client is not None:
            return client

        connect = getattr(account, "connect", None)
        if callable(connect):
            try:
                client = await connect()
                if client is not None:
                    account.client = client
                    return client
            except Exception as exc:
                self.logger.warning("ensure_client_failed", session_name=getattr(account, 'session_name', None), error=str(exc))
        return None

    def _extract_tracking_code(self, command: str) -> Optional[str]:
        """Extract tracking code from /start command."""
        parts = command.split(maxsplit=1)
        if len(parts) < 2:
            return None

        payload = parts[1].strip()
        if not payload:
            return None

        if payload.startswith("http"):
            parsed = urlparse(payload)
            query = parse_qs(parsed.query)
            return query.get("start", [None])[0] or query.get("ref", [None])[0]

        return payload

    async def _get_xboard_status(self, user_id: int) -> Optional[str]:
        """Get user status from XBoard integration if available."""
        integration = getattr(self, "xboard_client", None)
        if integration and hasattr(integration, "get_user_status"):
            try:
                status = await integration.get_user_status(user_id)
                if isinstance(status, dict):
                    return status.get("status") or status.get("state")
                return str(status)
            except Exception as exc:
                self.logger.warning("xboard_status_failed", user_id=user_id, error=str(exc))
        return None

    async def _check_send_interval(self, user_id: int) -> bool:
        """Check if enough time has passed since last message to this user."""
        cache = RedisCache()
        limiter = RateLimiter(key_prefix="acquisition:private:")

        allowed = await limiter.check(str(user_id), rate=6, period=3600)
        if not allowed:
            return False

        last_sent = await cache.get(f"acquisition:last_private:{user_id}")
        if last_sent:
            try:
                elapsed = datetime.utcnow().timestamp() - float(last_sent)
                if elapsed < 30:
                    return False
            except ValueError:
                pass

        await cache.set(f"acquisition:last_private:{user_id}", str(datetime.utcnow().timestamp()), ttl=24 * 3600)
        return True
