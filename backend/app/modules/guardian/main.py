"""
Guardian Bot

Main entry point for the Telegram guardian bot.
Integrates all guardian modules for group moderation.
"""

from dataclasses import dataclass
from typing import Optional

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.keyword.engine import KeywordEngine
from app.modules.guardian.anti_spam.competitor_block import CompetitorBlocker
from app.modules.guardian.anti_spam.spam_detector import SpamDetector
from app.modules.guardian.broadcast.broadcaster import GuardianBroadcaster
from app.modules.guardian.campaign_runner import ManagedGroupCampaignRunner
from app.modules.guardian.config import get_guardian_config
from app.modules.guardian.coupon.coupon_distributor import CouponDistributor
from app.modules.guardian.models import GroupCampaignTriggerEvent, ViolationAction
from app.modules.guardian.moderation.action_executor import ActionExecutor
from app.modules.guardian.moderation.rule_engine import EvaluationResult, GuardianRuleEngine
from app.modules.guardian.punishment.punishment_mgr import PunishmentManager
from app.modules.guardian.punishment.warn_system import WarnSystem
from app.modules.guardian.verification.captcha_gen import CaptchaGenerator
from app.modules.guardian.verification.verification_mgr import VerificationManager

logger = structlog.get_logger()


@dataclass
class GuardianContext:
    """Context containing all guardian components."""
    rule_engine: GuardianRuleEngine
    action_executor: ActionExecutor
    spam_detector: SpamDetector
    competitor_blocker: CompetitorBlocker
    punishment_manager: PunishmentManager
    warn_system: WarnSystem
    verification_manager: VerificationManager
    captcha_generator: CaptchaGenerator
    broadcaster: GuardianBroadcaster
    coupon_distributor: CouponDistributor
    campaign_runner: ManagedGroupCampaignRunner


class GuardianBot:
    """
    Telegram Guardian Bot.
    
    Main class that integrates all guardian modules for group moderation:
    - Message moderation (rule engine)
    - Anti-spam (frequency, repeated content)
    - Competitor blocking
    - User punishment
    - Group verification
    - Broadcasting
    """
    
    def __init__(
        self,
        db: AsyncSession,
        telegram_client=None,
        redis_client=None,
        xboard_client=None
    ):
        """
        Initialize GuardianBot.
        
        Args:
            db: Database session
            telegram_client: Telegram client for actions
            redis_client: Redis client for rate limiting
            xboard_client: XBoard client for rewards
        """
        self._db = db
        self._telegram_client = telegram_client
        self._config = get_guardian_config()
        
        keyword_engine = KeywordEngine(db)
        
        self._context = GuardianContext(
            rule_engine=GuardianRuleEngine(db, keyword_engine),
            action_executor=ActionExecutor(telegram_client),
            spam_detector=SpamDetector(redis_client),
            competitor_blocker=CompetitorBlocker(db, keyword_engine),
            punishment_manager=PunishmentManager(db),
            warn_system=WarnSystem(ActionExecutor(telegram_client)),
            verification_manager=VerificationManager(db, redis_client),
            captcha_generator=CaptchaGenerator(),
            broadcaster=GuardianBroadcaster(db, telegram_client),
            coupon_distributor=CouponDistributor(db, xboard_client),
            campaign_runner=ManagedGroupCampaignRunner(db, xboard_client),
        )
        
        self.logger = logger.bind(module="guardian_bot")
    
    async def initialize(self) -> None:
        """Initialize all components."""
        await self._context.rule_engine.load_rules()
        await self._context.competitor_blocker.reload()
        self.logger.info("guardian_bot_initialized")
    
    async def handle_message(
        self,
        message_id: int,
        chat_id: int,
        user_id: int,
        username: Optional[str],
        text: str
    ) -> bool:
        """
        Handle an incoming group message.
        
        Args:
            message_id: Message ID
            chat_id: Chat/Group ID
            user_id: User ID
            username: Username
            text: Message text
            
        Returns:
            True if message was processed
        """
        try:
            if not text:
                return False

            if self._is_private_chat(chat_id, user_id):
                return await self._handle_private_message(
                    chat_id=chat_id,
                    user_id=user_id,
                    username=username,
                    text=text,
                )
            
            is_verified = await self._context.verification_manager.is_user_verified(user_id, chat_id)
            
            config = await self._context.verification_manager.get_verification_config(chat_id)
            needs_verification = config and config.enable_verification
            
            if needs_verification and not is_verified:
                self.logger.debug("user_not_verified", user_id=user_id, chat_id=chat_id)
                return False
            
            spam_results = await self._context.spam_detector.check_all(user_id, chat_id, text)
            
            if spam_results:
                self.logger.warning(
                    "spam_detected",
                    user_id=user_id,
                    chat_id=chat_id,
                    spam_types=[r.spam_type for r in spam_results]
                )
            
            evaluation = await self._context.rule_engine.evaluate_message(
                text=text,
                user_id=user_id,
                group_id=chat_id,
                sender_username=username
            )
            
            if evaluation.is_violation:
                await self._handle_violation(
                    evaluation=evaluation,
                    message_id=message_id,
                    chat_id=chat_id,
                    user_id=user_id,
                    username=username,
                    text=text
                )
                return True
            
            return False
            
        except Exception as e:
            self.logger.error(
                "handle_message_error",
                error=str(e),
                message_id=message_id
            )
            return False

    async def _handle_private_message(
        self,
        *,
        chat_id: int,
        user_id: int,
        username: Optional[str],
        text: str,
    ) -> bool:
        if not text.strip().startswith("/start"):
            return False

        response = await self._context.campaign_runner.claim_group_coupon(
            text,
            user_telegram_id=user_id,
            username=username,
        )
        if not response:
            return False
        if self._telegram_client is None:
            self.logger.warning("private_claim_response_client_missing", user_id=user_id)
            return True

        await self._telegram_client.send_message(chat_id, response, parse_mode="")
        return True

    @staticmethod
    def _is_private_chat(chat_id: int, user_id: int) -> bool:
        return int(chat_id) == int(user_id)
    
    async def _handle_violation(
        self,
        evaluation: EvaluationResult,
        message_id: int,
        chat_id: int,
        user_id: int,
        username: Optional[str],
        text: str
    ) -> None:
        """Handle a detected violation."""
        matched_rule = evaluation.matched_rules[0] if evaluation.matched_rules else None
        
        rule_type = matched_rule.rule_type.value if matched_rule else "unknown"
        action = evaluation.recommended_action
        
        punishment = await self._context.punishment_manager.calculate_punishment(
            user_id=user_id,
            group_id=chat_id,
            level=evaluation.severity,
            is_repeat=len(evaluation.matched_rules) > 1
        )
        
        if punishment.action != ViolationAction.WARN or action == ViolationAction.WARN:
            action = punishment.action
        
        duration = punishment.duration
        
        await self._context.punishment_manager.record_violation(
            user_id=user_id,
            group_id=chat_id,
            rule_id=matched_rule.rule_id if matched_rule else None,
            rule_type=rule_type,
            content=text[:500] if text else None,
            action=action,
            duration=duration
        )
        
        warning_count = await self._context.punishment_manager.get_warning_count(user_id, chat_id)
        
        await self._context.action_executor.execute(
            action=action,
            chat_id=chat_id,
            user_id=user_id,
            message_id=message_id,
            duration=duration
        )
        
        await self._context.warn_system.send_warning(
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            level=evaluation.severity,
            violation_type=rule_type,
            warning_count=warning_count
        )
        
        self.logger.info(
            "violation_handled",
            user_id=user_id,
            chat_id=chat_id,
            action=action.value,
            severity=evaluation.severity.value
        )
    
    async def handle_new_member(
        self,
        chat_id: int,
        user_id: int,
        username: Optional[str]
    ) -> Optional[str]:
        """
        Handle a new member joining a group.
        
        Args:
            chat_id: Chat/Group ID
            user_id: User ID
            username: Username
            
        Returns:
            Message to send or None
        """
        try:
            result = await self._context.verification_manager.handle_new_member(
                user_id=user_id,
                chat_id=chat_id,
                username=username
            )

            await self._context.campaign_runner.trigger_for_event(
                event=GroupCampaignTriggerEvent.USER_JOINED,
                telegram_group_id=chat_id,
                user_telegram_id=user_id,
                username=username,
            )
            
            if result.action == "verify":
                if result.verification_type == "captcha":
                    captcha = self._context.captcha_generator.generate()
                    await self._context.verification_manager.generate_captcha_for_session(
                        result.session_id,
                        captcha.code
                    )
                    
                    return f"""🔐 *入群验证*

请输入下方显示的验证码完成验证：

`{captcha.code}`

⏰ 请在 5 分钟内完成验证
🔄 最多尝试 3 次"""
                else:
                    return result.message
            
            elif result.action == "welcome":
                return result.message

            return None
            
        except Exception as e:
            self.logger.error(
                "handle_new_member_error",
                error=str(e),
                user_id=user_id,
                chat_id=chat_id
            )
            return None
    
    async def handle_verification_answer(
        self,
        session_id: str,
        answer: str,
        chat_id: int
    ) -> str:
        """
        Handle verification answer.
        
        Args:
            session_id: Session ID
            answer: User's answer
            chat_id: Chat ID
            
        Returns:
            Response message
        """
        try:
            result = await self._context.verification_manager.verify_answer(session_id, answer)
            
            if result.success:
                session = await self._context.verification_manager.get_session(session_id)
                if session:
                    await self._context.campaign_runner.trigger_for_event(
                        event=GroupCampaignTriggerEvent.VERIFICATION_PASSED,
                        telegram_group_id=chat_id,
                        user_telegram_id=session.user_id,
                    )
                return "✅ 验证成功！欢迎加入群聊。"
            else:
                return f"❌ {result.message}"
                
        except Exception as e:
            self.logger.error(
                "handle_verification_error",
                error=str(e),
                session_id=session_id
            )
            return "验证过程出错，请重试。"
    
    async def handle_member_leave(
        self,
        chat_id: int,
        user_id: int
    ) -> None:
        """
        Handle a member leaving the group.
        
        Args:
            chat_id: Chat/Group ID
            user_id: User ID
        """
        self.logger.info("member_left", user_id=user_id, chat_id=chat_id)
    
    async def broadcast_node_status(
        self,
        group_ids: list[int],
        node_name: str,
        status: str,
        reason: Optional[str] = None,
        eta: Optional[str] = None
    ) -> dict:
        """
        Broadcast node status to groups.
        
        Args:
            group_ids: List of group IDs
            node_name: Node name
            status: Status (online/offline)
            reason: Offline reason
            eta: Estimated recovery time
            
        Returns:
            Broadcast result
        """
        from datetime import datetime

        from app.modules.guardian.broadcast.templates import NodeStatus
        
        node_status = NodeStatus(
            node_name=node_name,
            status=status,
            timestamp=datetime.now(),
            reason=reason,
            eta=eta
        )
        
        result = await self._context.broadcaster.broadcast_node_status(group_ids, node_status)
        
        return {
            "success": result.success,
            "failed": result.failed
        }
    
    async def get_user_punishment_summary(
        self,
        user_id: int,
        group_id: Optional[int] = None
    ) -> dict:
        """Get user punishment summary."""
        return await self._context.punishment_manager.get_user_punishment_summary(user_id, group_id)
    
    async def cleanup(self) -> None:
        """Cleanup resources."""
        count = await self._context.verification_manager.cleanup_expired_sessions()
        self.logger.info("cleanup_completed", expired_sessions=count)


async def create_guardian_bot(
    db: AsyncSession,
    telegram_client=None,
    redis_client=None,
    xboard_client=None
) -> GuardianBot:
    """
    Create and initialize a GuardianBot.
    
    Args:
        db: Database session
        telegram_client: Telegram client
        redis_client: Redis client
        xboard_client: XBoard client
        
    Returns:
        Initialized GuardianBot
    """
    bot = GuardianBot(
        db=db,
        telegram_client=telegram_client,
        redis_client=redis_client,
        xboard_client=xboard_client
    )
    await bot.initialize()
    return bot
