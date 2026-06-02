"""
User Tracker Module

Tracks user behavior and manages user sessions.

Features:
- User session management
- Behavior tracking
- Warning and mute tracking
- User statistics
"""

from datetime import datetime, timedelta
from typing import Optional

import structlog
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.user.models import User, UserState
from app.core.user.fsm import UserFSM, UserEvent
from app.core.exceptions import UserNotFoundError

logger = structlog.get_logger()


class UserTracker:
    """
    User behavior tracker and session manager.

    Tracks user actions, manages warnings/mutes, and provides
    user statistics for analytics.
    """

    def __init__(self, db: AsyncSession):
        """
        Initialize UserTracker.

        Args:
            db: SQLAlchemy async session
        """
        self.db = db
        self.fsm = UserFSM()
        self.logger = logger.bind(module="user_tracker")

    async def get_or_create_user(
        self,
        telegram_id: int,
        username: Optional[str] = None,
    ) -> User:
        """
        Get existing user or create new one.

        Args:
            telegram_id: Telegram user ID
            username: Optional username

        Returns:
            User instance
        """
        user = await self.get_user_by_telegram_id(telegram_id)

        if user:
            if username and user.username != username:
                user.username = username
                await self.db.commit()

            return user

        user = User(
            telegram_id=telegram_id,
            username=username,
            state=UserState.NEW,
        )

        self.db.add(user)
        await self.db.commit()
        await self.db.refresh(user)

        self.logger.info(
            "user_created",
            user_id=user.id,
            telegram_id=telegram_id,
        )

        await self.fsm.transition(user, UserEvent.REGISTER)

        return user

    async def get_user(self, user_id: int) -> Optional[User]:
        """
        Get user by database ID.

        Args:
            user_id: User database ID

        Returns:
            User if found, None otherwise
        """
        result = await self.db.execute(
            select(User).where(User.id == user_id)
        )
        return result.scalar_one_or_none()

    async def get_user_by_telegram_id(self, telegram_id: int) -> Optional[User]:
        """
        Get user by Telegram ID.

        Args:
            telegram_id: Telegram user ID

        Returns:
            User if found, None otherwise
        """
        result = await self.db.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        return result.scalar_one_or_none()

    async def list_users(
        self,
        state: Optional[UserState] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[User]:
        """
        List users with optional state filter.

        Args:
            state: Optional state filter
            limit: Max results
            offset: Pagination offset

        Returns:
            List of users
        """
        query = select(User)

        if state:
            query = query.where(User.state == state)

        query = query.order_by(User.created_at.desc())
        query = query.limit(limit).offset(offset)

        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def update_user_state(
        self,
        user_id: int,
        event: UserEvent,
    ) -> User:
        """
        Update user state based on event.

        Args:
            user_id: User database ID
            event: State transition event

        Returns:
            Updated User

        Raises:
            UserNotFoundError: If user not found
        """
        user = await self.get_user(user_id)
        if not user:
            raise UserNotFoundError(f"User {user_id} not found")

        success, new_state = await self.fsm.transition(user, event)

        if success:
            await self.db.commit()
            await self.db.refresh(user)

        return user

    async def start_trial(self, user_id: int) -> User:
        """
        Start trial for user.

        Args:
            user_id: User database ID

        Returns:
            Updated User
        """
        user = await self.get_user(user_id)
        if not user:
            raise UserNotFoundError(f"User {user_id} not found")

        user.trial_started_at = datetime.utcnow()
        user.trial_expires_at = datetime.utcnow() + timedelta(hours=24)

        await self.fsm.transition(user, UserEvent.TRIAL_STARTED)
        await self.db.commit()
        await self.db.refresh(user)

        self.logger.info(
            "trial_started",
            user_id=user_id,
            expires_at=user.trial_expires_at,
        )

        return user

    async def add_warning(self, user_id: int) -> User:
        """
        Add warning to user.

        Args:
            user_id: User database ID

        Returns:
            Updated User
        """
        user = await self.get_user(user_id)
        if not user:
            raise UserNotFoundError(f"User {user_id} not found")

        user.warning_count += 1
        await self.db.commit()
        await self.db.refresh(user)

        self.logger.info(
            "warning_added",
            user_id=user_id,
            warning_count=user.warning_count,
        )

        return user

    async def clear_warnings(self, user_id: int) -> User:
        """
        Clear all warnings for user.

        Args:
            user_id: User database ID

        Returns:
            Updated User
        """
        user = await self.get_user(user_id)
        if not user:
            raise UserNotFoundError(f"User {user_id} not found")

        user.warning_count = 0
        await self.db.commit()
        await self.db.refresh(user)

        self.logger.info("warnings_cleared", user_id=user_id)

        return user

    async def mute_user(
        self,
        user_id: int,
        duration_seconds: int,
    ) -> User:
        """
        Mute user for duration.

        Args:
            user_id: User database ID
            duration_seconds: Mute duration in seconds

        Returns:
            Updated User
        """
        user = await self.get_user(user_id)
        if not user:
            raise UserNotFoundError(f"User {user_id} not found")

        user.muted_until = datetime.utcnow() + timedelta(seconds=duration_seconds)
        await self.db.commit()
        await self.db.refresh(user)

        self.logger.info(
            "user_muted",
            user_id=user_id,
            duration_seconds=duration_seconds,
            muted_until=user.muted_until,
        )

        return user

    async def unmute_user(self, user_id: int) -> User:
        """
        Unmute user.

        Args:
            user_id: User database ID

        Returns:
            Updated User
        """
        user = await self.get_user(user_id)
        if not user:
            raise UserNotFoundError(f"User {user_id} not found")

        user.muted_until = None
        await self.db.commit()
        await self.db.refresh(user)

        self.logger.info("user_unmuted", user_id=user_id)

        return user

    async def is_muted(self, user_id: int) -> bool:
        """
        Check if user is currently muted.

        Args:
            user_id: User database ID

        Returns:
            True if muted
        """
        user = await self.get_user(user_id)
        if not user:
            return False

        if user.muted_until is None:
            return False

        return datetime.utcnow() < user.muted_until

    async def block_user(self, user_id: int) -> User:
        """
        Block user.

        Args:
            user_id: User database ID

        Returns:
            Updated User
        """
        user = await self.get_user(user_id)
        if not user:
            raise UserNotFoundError(f"User {user_id} not found")

        await self.fsm.transition(user, UserEvent.BLOCKED)
        await self.db.commit()
        await self.db.refresh(user)

        self.logger.info("user_blocked", user_id=user_id)

        return user

    async def unblock_user(self, user_id: int) -> User:
        """
        Unblock user.

        Args:
            user_id: User database ID

        Returns:
            Updated User
        """
        user = await self.get_user(user_id)
        if not user:
            raise UserNotFoundError(f"User {user_id} not found")

        await self.fsm.transition(user, UserEvent.UNBLOCKED)
        await self.db.commit()
        await self.db.refresh(user)

        self.logger.info("user_unblocked", user_id=user_id)

        return user

    async def get_user_statistics(self) -> dict:
        """
        Get overall user statistics.

        Returns:
            Dictionary with statistics
        """
        result = await self.db.execute(
            select(
                func.count(User.id).label("total"),
                func.avg(User.warning_count).label("avg_warnings"),
            )
        )
        row = result.one()

        state_counts = {}
        for state in UserState:
            count_result = await self.db.execute(
                select(func.count(User.id)).where(User.state == state)
            )
            state_counts[state.value] = count_result.scalar()

        return {
            "total_users": row.total or 0,
            "average_warnings": float(row.avg_warnings or 0),
            "by_state": state_counts,
        }

    async def get_users_needing_attention(self) -> dict:
        """
        Get users who need attention based on their state.

        Returns:
            Dictionary with lists of users by attention type
        """
        now = datetime.utcnow()

        trial_expiring = await self.db.execute(
            select(User).where(
                User.trial_expires_at.isnot(None),
                User.trial_expires_at <= now + timedelta(hours=12),
                User.trial_expires_at > now,
            )
        )

        trial_expired = await self.db.execute(
            select(User).where(
                User.state == UserState.PENDING,
                User.trial_expires_at < now,
            )
        )

        muted_users = await self.db.execute(
            select(User).where(
                User.muted_until.isnot(None),
                User.muted_until < now,
            )
        )

        warned_users = await self.db.execute(
            select(User).where(User.warning_count > 0)
        )

        return {
            "trial_expiring_soon": list(trial_expiring.scalars()),
            "trial_expired": list(trial_expired.scalars()),
            "muted_expired": list(muted_users.scalars()),
            "has_warnings": list(warned_users.scalars()),
        }
