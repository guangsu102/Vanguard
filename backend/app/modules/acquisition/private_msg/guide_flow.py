"""
Guide Flow Manager Module

Manages user guide flows through registration process.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import RedisCache
from app.modules.acquisition.constants import DEFAULT_GUIDE_MESSAGES, GUIDE_STEP_TIMEOUTS
from app.modules.acquisition.tracking.url_builder import URLBuilder

logger = structlog.get_logger()


class GuideStep(str, Enum):
    """Guide flow step identifiers."""
    WELCOME = "welcome"
    INTRODUCE = "introduce"
    INVITE_REGISTER = "invite_register"
    CONFIRM = "confirm"


@dataclass
class GuideStepConfig:
    """Configuration for a guide step."""
    step: GuideStep
    message: str
    timeout_seconds: int
    next_step: Optional["GuideStep"] = None
    is_final: bool = False


# 引导流程配置
GUIDE_FLOW_CONFIG = [
    GuideStepConfig(
        step=GuideStep.WELCOME,
        message=DEFAULT_GUIDE_MESSAGES.get(GuideStep.WELCOME, "很高兴认识你！有什么可以帮助你的吗？"),
        timeout_seconds=GUIDE_STEP_TIMEOUTS[GuideStep.WELCOME],
        next_step=GuideStep.INTRODUCE,
    ),
    GuideStepConfig(
        step=GuideStep.INTRODUCE,
        message=DEFAULT_GUIDE_MESSAGES.get(GuideStep.INTRODUCE, "XBoard 提供全球节点覆盖的高速网络加速服务，支持多设备同时使用。"),
        timeout_seconds=GUIDE_STEP_TIMEOUTS[GuideStep.INTRODUCE],
        next_step=GuideStep.INVITE_REGISTER,
    ),
    GuideStepConfig(
        step=GuideStep.INVITE_REGISTER,
        message=DEFAULT_GUIDE_MESSAGES.get(GuideStep.INVITE_REGISTER, "新用户有免费试用机会，点击这里注册：{register_link}"),
        timeout_seconds=GUIDE_STEP_TIMEOUTS[GuideStep.INVITE_REGISTER],
        next_step=GuideStep.CONFIRM,
    ),
    GuideStepConfig(
        step=GuideStep.CONFIRM,
        message=DEFAULT_GUIDE_MESSAGES.get(GuideStep.CONFIRM, "注册成功后会获得试用时长，快去体验吧！"),
        timeout_seconds=GUIDE_STEP_TIMEOUTS[GuideStep.CONFIRM],
        next_step=None,
        is_final=True,
    ),
]


class GuideFlowManager:
    """
    Manages user guide flows through the registration process.

    Coordinates multi-step onboarding with timeout handling
    and state persistence.
    """

    def __init__(
        self,
        db: AsyncSession,
        url_builder: Optional[URLBuilder] = None,
        expire_hours: int = 24,
    ):
        """
        Initialize GuideFlowManager.

        Args:
            db: Database session
            url_builder: Optional URL builder for tracking links
            expire_hours: Flow expiration time in hours
        """
        self.db = db
        self.url_builder = url_builder or URLBuilder()
        self.expire_hours = expire_hours
        self.logger = logger.bind(module="guide_flow_manager")

    async def start_flow(
        self,
        user_id: int,
        source_info: Optional[dict] = None,
    ) -> str:
        """
        Start a new guide flow for a user.

        Args:
            user_id: User ID
            source_info: Optional source information

        Returns:
            First step message
        """
        self.logger.info("starting_guide_flow", user_id=user_id)

        # 保存引导流状态
        await self._save_flow_state(user_id, GuideStep.WELCOME, source_info)
        await self._sync_dialog_state(user_id, state="init", current_step=0, source_info=source_info)

        # 返回第一步消息
        return await self.get_step_message(GuideStep.WELCOME, user_id=user_id)

    async def handle_step(
        self,
        user_id: int,
        user_message: str,
        context: Optional[dict] = None,
    ) -> dict:
        """
        Handle user response in guide flow.

        Args:
            user_id: User ID
            user_message: User's message
            context: Optional conversation context

        Returns:
            Dict with reply and completion status
        """
        # 获取当前状态
        current_step = await self._get_current_step(user_id)
        if not current_step:
            current_step = GuideStep.WELCOME

        self.logger.info("handling_guide_step", user_id=user_id, step=current_step)

        # 检查超时
        if await self._is_step_timeout(user_id, current_step):
            self.logger.info("step_timeout", user_id=user_id, step=current_step)
            # 超时后重新开始
            return await self._handle_timeout(user_id)

        # 根据用户消息决定下一步
        reply, next_step = await self._process_step_response(user_id, current_step, user_message)

        # 更新状态
        if next_step:
            await self._save_flow_state(user_id, next_step)
            await self._sync_dialog_state(user_id, state="awaiting_registration", current_step=self._get_step_index(next_step), source_info=context)

        return {
            "reply": reply,
            "current_step": next_step.value if next_step else None,
            "completed": next_step is None,
        }

    async def get_step_message(
        self,
        step: GuideStep,
        **kwargs,
    ) -> str:
        """
        Get message for a specific step.

        Args:
            step: Step identifier
            **kwargs: Template variables

        Returns:
            Step message
        """
        config = self._get_step_config(step)
        if not config:
            return "感谢您的关注！"

        message = config.message

        # 替换变量
        if "{register_link}" in message and "register_link" not in kwargs:
            user_id = kwargs.get("user_id", 0)
            kwargs["register_link"] = await self.url_builder.build_invite_url(user_id, "guide_flow")

        for key, value in kwargs.items():
            message = message.replace(f"{{{{{key}}}}}", str(value))

        return message

    async def advance_step(self, user_id: int) -> Optional[str]:
        """
        Advance to the next step.

        Args:
            user_id: User ID

        Returns:
            Next step message or None if flow complete
        """
        current_step = await self._get_current_step(user_id)
        if not current_step:
            return None

        config = self._get_step_config(current_step)
        if not config or not config.next_step:
            # 流程完成
            await self._complete_flow(user_id)
            return None

        # 移动到下一步
        await self._save_flow_state(user_id, config.next_step)
        return await self.get_step_message(config.next_step)

    async def skip_to_step(
        self,
        user_id: int,
        step: GuideStep,
    ) -> str:
        """
        Skip to a specific step.

        Args:
            user_id: User ID
            step: Target step

        Returns:
            Step message
        """
        await self._save_flow_state(user_id, step)
        return await self.get_step_message(step, user_id=user_id)

    async def _process_step_response(
        self,
        user_id: int,
        current_step: GuideStep,
        user_message: str,
    ) -> tuple[str, Optional[GuideStep]]:
        """Process user response and determine next step."""
        text_lower = user_message.lower().strip()

        # 根据当前步骤处理响应
        if current_step == GuideStep.WELCOME:
            return await self.advance_step(user_id), None

        elif current_step == GuideStep.INTRODUCE:
            # 用户可能有疑问或想继续
            if any(kw in text_lower for kw in ["了解", "看看", "好的", "继续", "怎么"]):
                return await self.advance_step(user_id), None
            else:
                return "没问题！有其他问题随时问我~", None

        elif current_step == GuideStep.INVITE_REGISTER:
            # 检查用户是否要注册
            if any(kw in text_lower for kw in ["注册", "好", "试试", "link", "链接"]):
                link = await self.url_builder.build_invite_url(user_id, "guide_flow")
                return f"好的，点击这个链接注册：{link}", None
            elif any(kw in text_lower for kw in ["不需要", "不了", "谢谢"]):
                return "好的，没问题！有需要随时找我~", None
            else:
                return await self.advance_step(user_id), None

        elif current_step == GuideStep.CONFIRM:
            # 检查注册确认
            if any(kw in text_lower for kw in ["注册", "好了", "成功", "ok", "完成"]):
                await self._confirm_registration(user_id)
                return "太好了！恭喜注册成功，快去体验吧~ 有问题随时找我！", None
            else:
                link = await self.url_builder.build_invite_url(user_id, "guide_flow")
                return f"还没注册？点击链接完成注册：{link}", None

        return "好的~", None

    async def _handle_timeout(self, user_id: int) -> dict:
        """Handle step timeout."""
        link = await self.url_builder.build_invite_url(user_id, "guide_flow")
        message = f"看起来您可能有事离开了~ 有需要随时回来找我！\n\n点击链接注册体验：{link}"

        return {
            "reply": message,
            "current_step": None,
            "completed": False,
            "reason": "timeout",
        }

    async def _confirm_registration(self, user_id: int) -> None:
        """Confirm user registration."""
        xboard_client = getattr(self, "xboard_client", None)
        if xboard_client and hasattr(xboard_client, "confirm_registration"):
            try:
                await xboard_client.confirm_registration(user_id)
            except Exception as exc:
                self.logger.warning("confirm_registration_failed", user_id=user_id, error=str(exc))
        self.logger.info("registration_confirmed", user_id=user_id)

    async def _complete_flow(self, user_id: int) -> None:
        """Mark guide flow as complete."""
        await self._save_flow_state(user_id, None, completed=True)
        self.logger.info("guide_flow_completed", user_id=user_id)

    async def _get_current_step(self, user_id: int) -> Optional[GuideStep]:
        """Get current guide step for user."""
        cache = RedisCache()
        step = await cache.get(f"acquisition:guide:{user_id}:step")
        if not step:
            return None
        try:
            return GuideStep(step)
        except ValueError:
            return None

    async def _is_step_timeout(self, user_id: int, step: GuideStep) -> bool:
        """Check if current step has timed out."""
        cache = RedisCache()
        saved_at = await cache.get(f"acquisition:guide:{user_id}:updated_at")
        if not saved_at:
            return False

        try:
            elapsed = datetime.utcnow().timestamp() - float(saved_at)
        except ValueError:
            return False

        timeout = GUIDE_STEP_TIMEOUTS.get(step, 3600)
        return elapsed > timeout

    async def _save_flow_state(
        self,
        user_id: int,
        step: Optional[GuideStep],
        source_info: Optional[dict] = None,
        completed: bool = False,
    ) -> None:
        """Save guide flow state to database."""
        cache = RedisCache()
        if step is None:
            await cache.delete(f"acquisition:guide:{user_id}:step")
            await cache.delete(f"acquisition:guide:{user_id}:updated_at")
            return

        await cache.set(f"acquisition:guide:{user_id}:step", step.value, ttl=self.expire_hours * 3600)
        await cache.set(f"acquisition:guide:{user_id}:updated_at", str(datetime.utcnow().timestamp()), ttl=self.expire_hours * 3600)
        self.logger.debug("saving_flow_state", user_id=user_id, step=step.value if step else None)

    def _get_step_config(self, step: GuideStep) -> Optional[GuideStepConfig]:
        """Get configuration for a step."""
        for config in GUIDE_FLOW_CONFIG:
            if config.step == step:
                return config
        return None

    async def get_progress(self, user_id: int) -> dict:
        """
        Get user's guide flow progress.

        Args:
            user_id: User ID

        Returns:
            Progress information
        """
        current_step = await self._get_current_step(user_id)
        total_steps = len(GUIDE_FLOW_CONFIG)

        if not current_step:
            return {"progress": 0, "step": None, "total_steps": total_steps}

        current_index = next(
            (i for i, c in enumerate(GUIDE_FLOW_CONFIG) if c.step == current_step),
            0
        )

        return {
            "progress": int((current_index / total_steps) * 100),
            "step": current_step.value,
            "current_index": current_index,
            "total_steps": total_steps,
        }
