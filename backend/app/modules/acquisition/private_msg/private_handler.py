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
from app.core.account.risk_guard import AccountRiskGuard
from app.core.account.telegram_execution import TelegramExecutionService
from app.core.automation_settings import (
    get_private_reply_template_settings,
    is_private_messaging_enabled,
)
from app.core.redis import RateLimiter, RedisCache
from app.core.runtime_settings import (
    render_runtime_template,
)
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
        self.risk_guard = AccountRiskGuard(db)
        self.telegram_execution = TelegramExecutionService(self.risk_guard)
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
                    guide_reply = guide_result.get("reply")
                    if guide_reply:
                        success = await self.send_message(user_id, guide_reply, initiated_by_user=True)
                        return PrivateMessageResult(
                            success=success,
                            reply=guide_reply,
                            action_taken="guide_flow",
                        )

                # 处理普通对话
                reply = await self._generate_reply(user_id, message_text, context)
                if reply:
                    await self.send_message(user_id, reply, initiated_by_user=True)

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
            reply = await self._render_private_template(
                "unknownCommand",
                user_id,
                command=command,
            )
            success = await self.send_message(user_id, reply, initiated_by_user=True)
            return PrivateMessageResult(
                success=success,
                reply=reply,
                action_taken="unknown_command",
            )

    async def _handle_start(self, user_id: int, command: str = "/start") -> PrivateMessageResult:
        """Handle /start command."""
        # 解析追踪码
        tracking_code = self._extract_tracking_code(command)
        if tracking_code:
            await self.tracker.record_tracking_code(user_id=user_id, tracking_code=tracking_code)

        tracking_link = await self.tracker.generate_tracking_link(
            user_id,
            tracking_code=tracking_code,
        )
        welcome = await self._render_private_template(
            "startWelcome",
            user_id,
            command=command,
            register_link=tracking_link,
        )
        success = await self.send_message(user_id, welcome, initiated_by_user=True)

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
        reply = await self._render_private_template("help", user_id)
        success = await self.send_message(user_id, reply, initiated_by_user=True)
        return PrivateMessageResult(success=success, reply=reply, action_taken="help")

    async def _handle_register(self, user_id: int) -> PrivateMessageResult:
        """Handle /register command."""
        tracking_link = await self.tracker.generate_tracking_link(user_id)
        reply = await self._render_private_template(
            "register",
            user_id,
            register_link=tracking_link,
        )
        success = await self.send_message(user_id, reply, initiated_by_user=True)
        return PrivateMessageResult(success=success, reply=reply, action_taken="register_link")

    async def _handle_status(self, user_id: int) -> PrivateMessageResult:
        """Handle /status command."""
        status = await self._get_xboard_status(user_id)
        if status:
            reply = await self._render_private_template("statusFound", user_id, status=status)
        else:
            reply = await self._render_private_template("statusPending", user_id, status="")
        success = await self.send_message(user_id, reply, initiated_by_user=True)
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
            return await self._render_private_template("thanks", user_id, message_text=message_text)
        elif any(kw in text_lower for kw in ["怎么用", "如何使用", "帮助"]):
            return await self._render_private_template("usageHelp", user_id, message_text=message_text)
        elif any(kw in text_lower for kw in ["注册", "试用", "体验"]):
            return await self._get_register_prompt(user_id)
        elif any(kw in text_lower for kw in ["价格", "多少钱", "收费"]):
            return await self._render_private_template("priceIntent", user_id, message_text=message_text)
        elif any(kw in text_lower for kw in ["节点", "速度", "稳定"]):
            return await self._render_private_template("nodeIntent", user_id, message_text=message_text)

        # 默认回复
        return await self._render_private_template("default", user_id, message_text=message_text)

    async def _get_register_prompt(self, user_id: int) -> str:
        """Get registration prompt with tracking link."""
        tracking_link = await self.tracker.generate_tracking_link(user_id)
        return await self._render_private_template(
            "registerIntent",
            user_id,
            register_link=tracking_link,
        )

    async def _render_private_template(
        self,
        template_key: str,
        user_id: int,
        **variables,
    ) -> str:
        """Render a private reply template with common variables."""
        templates = await get_private_reply_template_settings(self.db)
        template = templates.get(template_key, "")
        if "{register_link}" in template and "register_link" not in variables:
            variables["register_link"] = await self.tracker.generate_tracking_link(user_id)

        merged_variables = {
            "user_id": user_id,
            "user_name": variables.pop("user_name", None) or "朋友",
            "message_text": variables.pop("message_text", ""),
            "command": variables.pop("command", ""),
            "status": variables.pop("status", ""),
            "keyword": variables.pop("keyword", ""),
            **variables,
        }
        return render_runtime_template(template, merged_variables)

    async def send_message(
        self,
        user_id: int,
        message: str,
        account_id: Optional[int] = None,
        *,
        initiated_by_user: bool = False,
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
        message = (message or "").strip()
        if not message:
            self.logger.info(
                "private_message_empty",
                user_id=user_id,
                account_id=account_id,
                initiated_by_user=initiated_by_user,
            )
            return False

        if not await is_private_messaging_enabled(self.db, initiated_by_user=initiated_by_user):
            self.logger.info(
                "private_messaging_paused",
                user_id=user_id,
                account_id=account_id,
                initiated_by_user=initiated_by_user,
                message_length=len(message),
            )
            return False

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

            success = await self.telegram_execution.send_private_message(
                account,
                user_id,
                message,
                initiated_by_user=initiated_by_user,
                source="private_handler",
            )
            msg_id = None
            self.logger.info("message_sent", user_id=user_id, message_id=msg_id)
            return success

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
        if not await is_private_messaging_enabled(self.db, initiated_by_user=False):
            self.logger.info(
                "private_messaging_paused",
                user_id=user_id,
                initiated_by_user=False,
                source="send_welcome",
            )
            return False

        source_info = source_info or {}
        tracking_link = await self.tracker.generate_tracking_link(
            user_id,
            source_type=source_info.get("source", "tg_private"),
            group_id=source_info.get("group_id"),
            keyword=source_info.get("keyword"),
            bot_id=source_info.get("bot_id"),
            campaign_name=source_info.get("campaign"),
            tracking_code=source_info.get("tracking_code") or source_info.get("ref"),
        )
        welcome = await self._render_private_template(
            "startWelcome",
            user_id,
            user_name=source_info.get("user_name") or "朋友",
            register_link=tracking_link,
        )
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
        if not await is_private_messaging_enabled(self.db, initiated_by_user=False):
            self.logger.info(
                "private_messaging_paused",
                user_id=user_id,
                initiated_by_user=False,
                source="send_guide_message",
            )
            return False

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
        if not await is_private_messaging_enabled(self.db, initiated_by_user=False):
            self.logger.info(
                "private_messaging_paused",
                user_id=user_id,
                initiated_by_user=False,
                source="send_registration_link",
            )
            return False

        if not tracking_code:
            tracking_code = str(user_id)

        link = await self.tracker.generate_tracking_link(user_id, tracking_code=tracking_code)
        message = await self._render_private_template(
            "register",
            user_id,
            register_link=link,
        )
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

        message = await self._render_private_template(
            "triggerInvite",
            user_id,
            register_link=tracking_link,
            keyword=keyword or "",
        )

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



