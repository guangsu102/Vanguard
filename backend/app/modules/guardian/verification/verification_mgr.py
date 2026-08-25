"""
Verification Manager

Manages group join verification process.
"""

import asyncio
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import structlog
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.guardian.config import get_guardian_config
from app.modules.guardian.models import (
    VerificationSession,
    VerificationType,
    VerificationState,
    GroupVerificationConfig,
    Whitelist,
)

logger = structlog.get_logger()


@dataclass
class JoinResult:
    """Result of join verification check."""
    action: str
    should_verify: bool
    message: Optional[str]
    session_id: Optional[str]
    verification_type: Optional[str]


@dataclass
class VerifyResult:
    """Result of verification attempt."""
    success: bool
    message: str
    remaining_attempts: int


class VerificationManager:
    """
    Manages group join verification.
    
    Handles captcha and question-based verification for new group members.
    """
    
    def __init__(self, db: AsyncSession, redis_client=None):
        """
        Initialize VerificationManager.
        
        Args:
            db: Database session
            redis_client: Optional Redis client for caching
        """
        self.db = db
        self._redis = redis_client
        self._config = get_guardian_config()
        self._lock = asyncio.Lock()
        self.logger = logger.bind(module="verification_manager")
    
    async def get_verification_config(
        self,
        group_id: int
    ) -> Optional[GroupVerificationConfig]:
        """
        Get verification config for a group.
        
        Args:
            group_id: Group ID
            
        Returns:
            GroupVerificationConfig or None
        """
        result = await self.db.execute(
            select(GroupVerificationConfig).where(
                GroupVerificationConfig.group_id == group_id
            )
        )
        return result.scalar_one_or_none()
    
    async def create_or_update_config(
        self,
        group_id: int,
        enable_verification: bool,
        verification_type: VerificationType = VerificationType.CAPTCHA,
        questions: Optional[list[dict]] = None,
        welcome_message: Optional[str] = None,
        timeout_minutes: int = 5,
        whitelist_bypass: bool = True
    ) -> GroupVerificationConfig:
        """
        Create or update verification config for a group.
        
        Args:
            group_id: Group ID
            enable_verification: Whether to enable verification
            verification_type: Type of verification
            questions: List of question-answer pairs
            welcome_message: Welcome message template
            timeout_minutes: Verification timeout
            whitelist_bypass: Whether to bypass whitelist users
            
        Returns:
            GroupVerificationConfig
        """
        config = await self.get_verification_config(group_id)
        
        if not config:
            config = GroupVerificationConfig(group_id=group_id)
            self.db.add(config)
        
        config.enable_verification = enable_verification
        config.verification_type = verification_type
        config.welcome_message = welcome_message or "欢迎 {username} 加入群聊！"
        config.timeout_minutes = timeout_minutes
        config.whitelist_bypass = whitelist_bypass
        
        if questions:
            config.set_questions(questions)
        
        await self.db.commit()
        await self.db.refresh(config)
        
        self.logger.info(
            "verification_config_updated",
            group_id=group_id,
            enabled=enable_verification
        )
        
        return config
    
    async def is_user_whitelisted(
        self,
        user_id: int,
        group_id: int
    ) -> bool:
        """
        Check if user is whitelisted.
        
        Args:
            user_id: User ID
            group_id: Group ID
            
        Returns:
            True if whitelisted
        """
        result = await self.db.execute(
            select(Whitelist).where(
                and_(
                    Whitelist.whitelist_type == "user",
                    Whitelist.value == str(user_id),
                    (Whitelist.group_id == group_id) | (Whitelist.group_id.is_(None))
                )
            )
        )
        whitelist = result.scalar_one_or_none()
        
        if not whitelist:
            return False
        
        if whitelist.expires_at and whitelist.expires_at < datetime.utcnow():
            return False
        
        return True
    
    async def should_verify(
        self,
        user_id: int,
        group_id: int
    ) -> tuple[bool, str]:
        """
        Determine if user should verify.
        
        Args:
            user_id: User ID
            group_id: Group ID
            
        Returns:
            (should_verify, reason)
        """
        config = await self.get_verification_config(group_id)
        
        if not config or not config.enable_verification:
            return False, "disabled"
        
        if config.whitelist_bypass:
            if await self.is_user_whitelisted(user_id, group_id):
                return False, "whitelist"
        
        return True, "required"
    
    async def handle_new_member(
        self,
        user_id: int,
        chat_id: int,
        username: Optional[str]
    ) -> JoinResult:
        """
        Handle new member joining a group.
        
        Args:
            user_id: User ID
            chat_id: Group ID
            username: Username
            
        Returns:
            JoinResult
        """
        should_verify, reason = await self.should_verify(user_id, chat_id)
        
        if not should_verify:
            display_name = username or f"User_{user_id}"
            
            config = await self.get_verification_config(chat_id)
            welcome_msg = config.welcome_message if config else "欢迎 {username} 加入群聊！"
            message = welcome_msg.format(username=display_name)
            
            return JoinResult(
                action="welcome",
                should_verify=False,
                message=message,
                session_id=None,
                verification_type=None
            )
        
        config = await self.get_verification_config(chat_id)
        verify_type = config.verification_type if config else VerificationType.CAPTCHA
        
        session = await self._create_verification_session(
            user_id=user_id,
            chat_id=chat_id,
            verify_type=verify_type,
            config=config
        )
        
        message = await self._build_verify_message(verify_type, session, config)
        
        return JoinResult(
            action="verify",
            should_verify=True,
            message=message,
            session_id=session.session_id,
            verification_type=verify_type.value
        )
    
    async def _create_verification_session(
        self,
        user_id: int,
        chat_id: int,
        verify_type: VerificationType,
        config: Optional[GroupVerificationConfig]
    ) -> VerificationSession:
        """Create a new verification session."""
        timeout = config.timeout_minutes if config else self._config.verification_timeout_minutes
        max_attempts = config.max_attempts if config else self._config.max_verification_attempts
        
        session_id = secrets.token_urlsafe(32)
        
        session = VerificationSession(
            session_id=session_id,
            user_id=user_id,
            chat_id=chat_id,
            verify_type=verify_type,
            state=VerificationState.PENDING,
            max_attempts=max_attempts,
            expires_at=datetime.utcnow() + timedelta(minutes=timeout)
        )
        
        if verify_type == VerificationType.QUESTION and config:
            questions = config.get_questions()
            if questions:
                import random
                selected = random.choice(questions)
                session.question = selected.get("question")
                session.answer = selected.get("answer")
        
        self.db.add(session)
        await self.db.commit()
        await self.db.refresh(session)
        
        self.logger.info(
            "verification_session_created",
            session_id=session_id,
            user_id=user_id,
            chat_id=chat_id,
            verify_type=verify_type.value
        )
        
        return session
    
    async def _build_verify_message(
        self,
        verify_type: VerificationType,
        session: VerificationSession,
        config: Optional[GroupVerificationConfig]
    ) -> str:
        """Build verification message."""
        timeout = config.timeout_minutes if config else self._config.verification_timeout_minutes
        
        if verify_type == VerificationType.CAPTCHA:
            return f"""🔐 *入群验证*

请输入下方显示的验证码完成验证：

`{session.captcha_code or 'CODE'}`

⏰ 请在 {timeout} 分钟内完成验证
🔄 最多尝试 {session.max_attempts} 次"""
        
        else:
            return f"""❓ *入群验证*

请回答以下问题：

*{session.question}*

⏰ 请在 {timeout} 分钟内回答
🔄 最多尝试 {session.max_attempts} 次"""
    
    async def verify_answer(
        self,
        session_id: str,
        answer: str
    ) -> VerifyResult:
        """
        Verify user's answer.
        
        Args:
            session_id: Session ID
            answer: User's answer
            
        Returns:
            VerifyResult
        """
        result = await self.db.execute(
            select(VerificationSession).where(
                VerificationSession.session_id == session_id
            )
        )
        session = result.scalar_one_or_none()
        
        if not session:
            return VerifyResult(
                success=False,
                message="验证会话不存在",
                remaining_attempts=0
            )
        
        if session.state != VerificationState.PENDING:
            return VerifyResult(
                success=False,
                message="验证已过期或已完成",
                remaining_attempts=0
            )
        
        if session.expires_at < datetime.utcnow():
            session.state = VerificationState.EXPIRED
            await self.db.commit()
            return VerifyResult(
                success=False,
                message="验证已超时，请重新入群",
                remaining_attempts=0
            )
        
        session.attempt_count += 1
        
        correct = False
        if session.verify_type == VerificationType.CAPTCHA:
            correct = answer.upper().strip() == (session.captcha_code or "").upper().strip()
        else:
            correct = answer.strip().lower() == (session.answer or "").strip().lower()
        
        if correct:
            session.state = VerificationState.PASSED
            session.completed_at = datetime.utcnow()
            await self.db.commit()
            
            self.logger.info(
                "verification_passed",
                session_id=session_id,
                user_id=session.user_id
            )
            
            return VerifyResult(
                success=True,
                message="验证成功！",
                remaining_attempts=session.max_attempts - session.attempt_count
            )
        
        remaining = session.max_attempts - session.attempt_count
        
        if remaining <= 0:
            session.state = VerificationState.FAILED
            await self.db.commit()
            
            self.logger.warning(
                "verification_failed",
                session_id=session_id,
                user_id=session.user_id
            )
            
            return VerifyResult(
                success=False,
                message="验证失败次数过多，请重新入群",
                remaining_attempts=0
            )
        
        await self.db.commit()
        
        return VerifyResult(
            success=False,
            message=f"验证码错误，还剩 {remaining} 次机会",
            remaining_attempts=remaining
        )

    async def get_session(self, session_id: str) -> Optional[VerificationSession]:
        """Get verification session by session id."""
        result = await self.db.execute(
            select(VerificationSession).where(VerificationSession.session_id == session_id)
        )
        return result.scalar_one_or_none()
    
    async def cleanup_expired_sessions(self) -> int:
        """
        Clean up expired verification sessions.
        
        Returns:
            Number of sessions cleaned up
        """
        result = await self.db.execute(
            select(VerificationSession).where(
                VerificationSession.state == VerificationState.PENDING,
                VerificationSession.expires_at < datetime.utcnow()
            )
        )
        expired = list(result.scalars().all())
        
        for session in expired:
            session.state = VerificationState.EXPIRED
        
        await self.db.commit()
        
        count = len(expired)
        if count > 0:
            self.logger.info("expired_sessions_cleaned", count=count)
        
        return count
    
    async def is_user_verified(
        self,
        user_id: int,
        chat_id: int
    ) -> bool:
        """
        Check if user has passed verification for the group.
        
        Args:
            user_id: User ID
            chat_id: Group ID
            
        Returns:
            True if user is verified
        """
        result = await self.db.execute(
            select(VerificationSession).where(
                and_(
                    VerificationSession.user_id == user_id,
                    VerificationSession.chat_id == chat_id,
                    VerificationSession.state == VerificationState.PASSED
                )
            ).order_by(VerificationSession.completed_at.desc())
        )
        session = result.scalar_one_or_none()
        
        return session is not None
    
    async def generate_captcha_for_session(
        self,
        session_id: str,
        captcha_code: str
    ) -> None:
        """
        Set captcha code for a session.
        
        Args:
            session_id: Session ID
            captcha_code: Generated captcha code
        """
        result = await self.db.execute(
            select(VerificationSession).where(
                VerificationSession.session_id == session_id
            )
        )
        session = result.scalar_one_or_none()
        
        if session:
            session.captcha_code = captcha_code
            await self.db.commit()
