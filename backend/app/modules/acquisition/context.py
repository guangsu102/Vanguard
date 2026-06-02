"""
Context Module

Manages cross-message context and state for conversations.
"""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.acquisition.models import ConversationContext as DBConversationContext

logger = structlog.get_logger()


@dataclass
class MessageContext:
    """In-memory message context."""
    user_id: int
    group_id: Optional[int]
    last_message_at: datetime
    last_message_id: Optional[int] = None
    metadata: dict = field(default_factory=dict)


class ContextManager:
    """
    Global context manager for acquisition operations.

    Maintains lightweight in-memory context for performance
    with database persistence for durability.
    """

    def __init__(
        self,
        db: AsyncSession,
        cache_ttl_minutes: int = 60,
    ):
        """
        Initialize ContextManager.

        Args:
            db: Database session
            cache_ttl_minutes: Context cache TTL in minutes
        """
        self.db = db
        self.cache_ttl = timedelta(minutes=cache_ttl_minutes)
        self._user_contexts: dict[int, MessageContext] = {}
        self._group_contexts: dict[int, list[MessageContext]] = {}
        self._lock = asyncio.Lock()
        self.logger = logger.bind(module="context_manager")

    async def get_user_context(
        self,
        user_id: int,
    ) -> Optional[MessageContext]:
        """
        Get context for a user.

        Args:
            user_id: User ID

        Returns:
            MessageContext or None
        """
        if user_id in self._user_contexts:
            ctx = self._user_contexts[user_id]
            if datetime.utcnow() - ctx.last_message_at < self.cache_ttl:
                return ctx
            else:
                del self._user_contexts[user_id]

        return None

    async def set_user_context(
        self,
        user_id: int,
        group_id: Optional[int] = None,
        metadata: Optional[dict] = None,
    ) -> MessageContext:
        """
        Set context for a user.

        Args:
            user_id: User ID
            group_id: Current group ID
            metadata: Optional metadata

        Returns:
            Updated MessageContext
        """
        if user_id in self._user_contexts:
            ctx = self._user_contexts[user_id]
            ctx.last_message_at = datetime.utcnow()
            if group_id:
                ctx.group_id = group_id
            if metadata:
                ctx.metadata.update(metadata)
        else:
            ctx = MessageContext(
                user_id=user_id,
                group_id=group_id,
                last_message_at=datetime.utcnow(),
                metadata=metadata or {},
            )
            self._user_contexts[user_id] = ctx

        return ctx

    async def update_user_metadata(
        self,
        user_id: int,
        **kwargs,
    ) -> Optional[MessageContext]:
        """
        Update user context metadata.

        Args:
            user_id: User ID
            **kwargs: Metadata fields to update

        Returns:
            Updated MessageContext or None
        """
        ctx = await self.get_user_context(user_id)
        if not ctx:
            return None

        ctx.metadata.update(kwargs)
        return ctx

    async def get_group_contexts(
        self,
        group_id: int,
    ) -> list[MessageContext]:
        """
        Get all user contexts for a group.

        Args:
            group_id: Group ID

        Returns:
            List of MessageContexts
        """
        return self._group_contexts.get(group_id, [])

    async def cleanup_expired(self) -> int:
        """
        Clean up expired contexts.

        Returns:
            Number of contexts cleaned up
        """
        now = datetime.utcnow()
        cleaned = 0

        expired_users = [
            uid for uid, ctx in self._user_contexts.items()
            if now - ctx.last_message_at >= self.cache_ttl
        ]

        for uid in expired_users:
            del self._user_contexts[uid]
            cleaned += 1

        self.logger.info("context_cleanup", cleaned=cleaned)
        return cleaned

    async def persist_context(self, user_id: int) -> None:
        """
        Persist user context to database.

        Args:
            user_id: User ID
        """
        ctx = await self.get_user_context(user_id)
        if not ctx:
            return

        try:
            # 获取或创建数据库记录
            result = await self.db.execute(
                select(DBConversationContext).where(
                    DBConversationContext.user_id == user_id
                )
            )
            db_ctx = result.scalar_one_or_none()

            context_data = json.dumps(ctx.metadata)

            if db_ctx:
                db_ctx.context_data = context_data
                db_ctx.expires_at = datetime.utcnow() + timedelta(minutes=60)
            else:
                db_ctx = DBConversationContext(
                    user_id=user_id,
                    context_data=context_data,
                    expires_at=datetime.utcnow() + timedelta(minutes=60),
                )
                self.db.add(db_ctx)

            await self.db.commit()
            self.logger.debug("context_persisted", user_id=user_id)

        except Exception as e:
            self.logger.error("context_persist_error", user_id=user_id, error=str(e))

    def get_statistics(self) -> dict:
        """Get context manager statistics."""
        return {
            "cached_users": len(self._user_contexts),
            "cached_groups": len(self._group_contexts),
        }
