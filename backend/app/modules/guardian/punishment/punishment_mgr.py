"""
Punishment Manager

Manages user punishment records and punishment strategies.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import structlog
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.guardian.config import get_guardian_config
from app.modules.guardian.models import (
    GroupPunishmentPolicy,
    Violation,
    ViolationAction,
    ViolationLevel,
)
from app.core.user.models import User

logger = structlog.get_logger()


@dataclass
class PunishmentResult:
    """Result of punishment calculation."""
    action: ViolationAction
    duration: Optional[int]
    reason: str
    should_escalate: bool


class PunishmentManager:
    """
    Manages user punishments and violation records.
    
    Tracks user violations and calculates appropriate punishment based on:
    - Violation severity
    - User's violation history
    - Configured punishment thresholds
    """
    
    def __init__(self, db: AsyncSession):
        """
        Initialize PunishmentManager.
        
        Args:
            db: Database session
        """
        self.db = db
        self._config = get_guardian_config()
        self.logger = logger.bind(module="punishment_manager")

    async def _get_policy(self, group_id: int) -> GroupPunishmentPolicy | None:
        result = await self.db.execute(
            select(GroupPunishmentPolicy).where(
                (GroupPunishmentPolicy.group_id == group_id) | (GroupPunishmentPolicy.group_id.is_(None))
            ).order_by(GroupPunishmentPolicy.group_id.desc())
        )
        return result.scalars().first()
    
    async def record_violation(
        self,
        user_id: int,
        group_id: int,
        rule_id: Optional[int],
        rule_type: str,
        content: Optional[str],
        action: ViolationAction,
        duration: Optional[int] = None
    ) -> Violation:
        """
        Record a violation.
        
        Args:
            user_id: User ID
            group_id: Group ID
            rule_id: Rule ID that was triggered
            rule_type: Type of rule
            content: Violation content
            action: Action taken
            duration: Duration of punishment (for mute)
            
        Returns:
            Created Violation record
        """
        violation = Violation(
            user_id=user_id,
            group_id=group_id,
            rule_type=rule_type,
            rule_pattern=str(rule_id) if rule_id else None,
            content=content,
            action_taken=action,
            action_duration=duration
        )
        
        self.db.add(violation)
        await self.db.commit()
        await self.db.refresh(violation)
        
        self.logger.info(
            "violation_recorded",
            violation_id=violation.id,
            user_id=user_id,
            group_id=group_id,
            action=action.value
        )
        
        return violation
    
    async def get_user_violations(
        self,
        user_id: int,
        group_id: Optional[int] = None,
        hours: int = 24,
        action: Optional[ViolationAction] = None
    ) -> list[Violation]:
        """
        Get user violations within a time window.
        
        Args:
            user_id: User ID
            group_id: Optional group ID filter
            hours: Time window in hours
            action: Optional action filter
            
        Returns:
            List of violations
        """
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        
        query = select(Violation).where(
            and_(
                Violation.user_id == user_id,
                Violation.created_at >= cutoff
            )
        )
        
        if group_id:
            query = query.where(Violation.group_id == group_id)
        
        if action:
            query = query.where(Violation.action_taken == action)
        
        query = query.order_by(Violation.created_at.desc())
        
        result = await self.db.execute(query)
        return list(result.scalars().all())
    
    async def get_warning_count(
        self,
        user_id: int,
        group_id: Optional[int] = None,
        hours: int = 24
    ) -> int:
        """
        Get warning count for a user.
        
        Args:
            user_id: User ID
            group_id: Optional group ID
            hours: Time window
            
        Returns:
            Number of warnings
        """
        violations = await self.get_user_violations(
            user_id=user_id,
            group_id=group_id,
            hours=hours,
            action=ViolationAction.WARN
        )
        return len(violations)
    
    async def get_mute_count(
        self,
        user_id: int,
        group_id: Optional[int] = None,
        hours: int = 168
    ) -> int:
        """
        Get mute count for a user.
        
        Args:
            user_id: User ID
            group_id: Optional group ID
            hours: Time window (default 7 days)
            
        Returns:
            Number of mutes
        """
        violations = await self.get_user_violations(
            user_id=user_id,
            group_id=group_id,
            hours=hours,
            action=ViolationAction.MUTE
        )
        return len(violations)
    
    async def calculate_punishment(
        self,
        user_id: int,
        group_id: int,
        level: ViolationLevel,
        is_repeat: bool = False
    ) -> PunishmentResult:
        """
        Calculate appropriate punishment based on user history and severity.
        
        Args:
            user_id: User ID
            group_id: Group ID
            level: Violation severity level
            is_repeat: Whether this is a repeated violation
            
        Returns:
            PunishmentResult with action and duration
        """
        policy = await self._get_policy(group_id)
        warning_count = await self.get_warning_count(user_id, group_id)
        mute_count = await self.get_mute_count(user_id, group_id)

        ban_threshold = policy.ban_on_warn_threshold if policy else self._config.ban_threshold
        warn_threshold = policy.warn_threshold if policy else self._config.warning_threshold
        mute_duration = policy.mute_duration_seconds if policy else self._config.mute_duration_seconds

        if mute_count >= ban_threshold:
            return PunishmentResult(
                action=ViolationAction.BAN,
                duration=None,
                reason="Exceeds ban threshold",
                should_escalate=False
            )

        if warning_count >= warn_threshold:
            return PunishmentResult(
                action=ViolationAction.MUTE,
                duration=mute_duration,
                reason="Warning threshold exceeded",
                should_escalate=True
            )
        
        if level == ViolationLevel.HIGH:
            duration = self._get_mute_duration(level)
            return PunishmentResult(
                action=ViolationAction.MUTE,
                duration=duration,
                reason="High severity violation",
                should_escalate=True
            )
        
        if level == ViolationLevel.MEDIUM:
            if is_repeat:
                duration = self._get_mute_duration(level)
                return PunishmentResult(
                    action=ViolationAction.MUTE,
                    duration=duration,
                    reason="Repeated medium violation",
                    should_escalate=True
                )
            return PunishmentResult(
                action=ViolationAction.WARN,
                duration=None,
                reason="First medium violation",
                should_escalate=True
            )
        
        if is_repeat:
            return PunishmentResult(
                action=ViolationAction.MUTE,
                duration=self._config.low_violation_mute_seconds,
                reason="Repeated low violation",
                should_escalate=True
            )
        
        return PunishmentResult(
            action=ViolationAction.WARN,
            duration=None,
            reason="First low violation",
            should_escalate=False
        )
    
    def _get_mute_duration(self, level: ViolationLevel) -> int:
        """Get mute duration based on violation level."""
        if level == ViolationLevel.HIGH:
            return self._config.high_violation_mute_seconds
        elif level == ViolationLevel.MEDIUM:
            return self._config.medium_violation_mute_seconds
        else:
            return self._config.low_violation_mute_seconds
    
    async def should_escalate(
        self,
        user_id: int,
        group_id: int
    ) -> bool:
        """
        Check if punishment should be escalated.
        
        Args:
            user_id: User ID
            group_id: Group ID
            
        Returns:
            True if escalation is warranted
        """
        warning_count = await self.get_warning_count(user_id, group_id)
        mute_count = await self.get_mute_count(user_id, group_id)
        policy = await self._get_policy(group_id)
        warn_threshold = policy.warn_threshold if policy else self._config.warning_threshold
        ban_threshold = policy.ban_on_warn_threshold if policy else self._config.ban_threshold
        
        return (
            warning_count >= warn_threshold or
            mute_count >= ban_threshold - 1
        )
    
    async def get_user_punishment_summary(
        self,
        user_id: int,
        group_id: Optional[int] = None
    ) -> dict:
        """
        Get a summary of user punishment history.
        
        Args:
            user_id: User ID
            group_id: Optional group ID
            
        Returns:
            Dict with punishment summary
        """
        violations_24h = await self.get_user_violations(user_id, group_id, hours=24)
        violations_7d = await self.get_user_violations(user_id, group_id, hours=168)
        
        return {
            "user_id": user_id,
            "group_id": group_id,
            "warnings_24h": len([v for v in violations_24h if v.action_taken == ViolationAction.WARN]),
            "warnings_7d": len([v for v in violations_7d if v.action_taken == ViolationAction.WARN]),
            "mutes_7d": len([v for v in violations_7d if v.action_taken == ViolationAction.MUTE]),
            "bans_7d": len([v for v in violations_7d if v.action_taken == ViolationAction.BAN]),
            "total_24h": len(violations_24h),
            "total_7d": len(violations_7d),
            "should_escalate": await self.should_escalate(user_id, group_id or 0)
        }
    
    async def update_user_mute_status(
        self,
        user_id: int,
        muted_until: Optional[datetime]
    ) -> bool:
        """
        Update user's mute status.
        
        Args:
            user_id: User ID
            muted_until: Mute expiration time
            
        Returns:
            True if updated
        """
        result = await self.db.execute(
            select(User).where(User.id == user_id)
        )
        user = result.scalar_one_or_none()
        
        if user:
            user.muted_until = muted_until
            await self.db.commit()
            return True
        
        return False
    
    async def check_user_muted(
        self,
        user_id: int
    ) -> tuple[bool, Optional[datetime]]:
        """
        Check if user is currently muted.
        
        Args:
            user_id: User ID
            
        Returns:
            (is_muted, mute_expires_at)
        """
        result = await self.db.execute(
            select(User).where(User.id == user_id)
        )
        user = result.scalar_one_or_none()
        
        if not user or not user.muted_until:
            return False, None
        
        if user.muted_until > datetime.utcnow():
            return True, user.muted_until
        
        user.muted_until = None
        await self.db.commit()
        return False, None
