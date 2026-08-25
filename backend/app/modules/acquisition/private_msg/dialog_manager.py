"""
Dialog Manager Module

Manages conversation state and context across multiple messages.
"""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.acquisition.models import ConversationContext as DBConversationContext

logger = structlog.get_logger()


class ConversationState(str, Enum):
    """Conversation state enum."""
    INIT = "init"
    AWAITING_REGISTRATION = "awaiting_registration"
    REGISTERED = "registered"
    CLOSED = "closed"


@dataclass
class ConversationData:
    """In-memory conversation data."""
    user_id: int
    state: ConversationState = ConversationState.INIT
    current_step: int = 0
    source_type: Optional[str] = None
    source_group_id: Optional[int] = None
    source_keyword: Optional[str] = None
    tracking_code: Optional[str] = None
    steps_completed: list[str] = field(default_factory=list)
    started_at: datetime = field(default_factory=datetime.utcnow)
    last_message_at: datetime = field(default_factory=datetime.utcnow)
    message_history: list[dict] = field(default_factory=list)


class DialogManager:
    """
    Manages user conversation state and context.

    Tracks conversation flow, maintains message history,
    and provides context for response generation.
    """

    def __init__(
        self,
        db: AsyncSession,
        expire_minutes: int = 60,
    ):
        """
        Initialize DialogManager.

        Args:
            db: Database session
            expire_minutes: Context expiration time in minutes
        """
        self.db = db
        self.expire_minutes = expire_minutes
        self._cache: dict[int, ConversationData] = {}
        self._lock = asyncio.Lock()
        self.logger = logger.bind(module="dialog_manager")

    async def get_or_create_context(
        self,
        user_id: int,
    ) -> ConversationData:
        """
        Get or create conversation context for a user.

        Args:
            user_id: User ID

        Returns:
            ConversationData for the user
        """
        # 检查内存缓存
        if user_id in self._cache:
            context = self._cache[user_id]
            context.last_message_at = datetime.utcnow()
            return context

        # 从数据库加载
        context = await self._load_from_db(user_id)
        if not context:
            context = ConversationData(user_id=user_id)
            await self._save_to_db(context)

        self._cache[user_id] = context
        return context

    async def update_context(
        self,
        user_id: int,
        **kwargs,
    ) -> Optional[ConversationData]:
        """
        Update conversation context.

        Args:
            user_id: User ID
            **kwargs: Fields to update

        Returns:
            Updated ConversationData or None
        """
        context = await self.get_or_create_context(user_id)

        for key, value in kwargs.items():
            if hasattr(context, key):
                setattr(context, key, value)

        context.last_message_at = datetime.utcnow()

        # 保存到数据库
        await self._save_to_db(context)

        return context

    async def add_message(
        self,
        user_id: int,
        role: str,
        content: str,
    ) -> None:
        """
        Add a message to conversation history.

        Args:
            user_id: User ID
            role: Message role (user/bot)
            content: Message content
        """
        context = await self.get_or_create_context(user_id)

        context.message_history.append({
            "role": role,
            "content": content,
            "timestamp": datetime.utcnow().isoformat(),
        })

        # 保持最多10条历史
        if len(context.message_history) > 10:
            context.message_history = context.message_history[-10:]

        await self._save_to_db(context)

    async def get_history(
        self,
        user_id: int,
        limit: int = 10,
    ) -> list[dict]:
        """
        Get conversation history for a user.

        Args:
            user_id: User ID
            limit: Maximum messages to return

        Returns:
            List of message dicts
        """
        context = await self.get_or_create_context(user_id)
        return context.message_history[-limit:]

    async def update_state(
        self,
        user_id: int,
        state: ConversationState,
    ) -> None:
        """
        Update conversation state.

        Args:
            user_id: User ID
            state: New state
        """
        await self.update_context(user_id, state=state)

    async def advance_step(self, user_id: int) -> None:
        """Advance to the next guide step."""
        context = await self.get_or_create_context(user_id)
        context.current_step += 1
        await self._save_to_db(context)

    async def complete_step(
        self,
        user_id: int,
        step: str,
    ) -> None:
        """
        Mark a step as completed.

        Args:
            user_id: User ID
            step: Step identifier
        """
        context = await self.get_or_create_context(user_id)
        if step not in context.steps_completed:
            context.steps_completed.append(step)
        await self._save_to_db(context)

    async def is_step_completed(self, user_id: int, step: str) -> bool:
        """Check if a step is completed."""
        context = await self.get_or_create_context(user_id)
        return step in context.steps_completed

    async def close_conversation(self, user_id: int) -> None:
        """Close a conversation."""
        await self.update_state(user_id, ConversationState.CLOSED)
        if user_id in self._cache:
            del self._cache[user_id]

    async def cleanup_expired(self) -> int:
        """
        Clean up expired contexts.

        Returns:
            Number of contexts cleaned up
        """
        expired_time = datetime.utcnow() - timedelta(minutes=self.expire_minutes)
        cleaned = 0

        for user_id in list(self._cache.keys()):
            context = self._cache[user_id]
            if context.last_message_at < expired_time:
                await self._save_to_db(context)
                del self._cache[user_id]
                cleaned += 1

        self.logger.info("cleanup_expired", cleaned=cleaned)
        return cleaned

    async def _load_from_db(self, user_id: int) -> Optional[ConversationData]:
        """Load context from database."""
        from sqlalchemy import select

        result = await self.db.execute(
            select(DBConversationContext).where(
                DBConversationContext.user_id == user_id
            )
        )
        db_context = result.scalar_one_or_none()

        if not db_context:
            return None

        context = ConversationData(
            user_id=user_id,
            state=ConversationState.INIT,
            current_step=0,
        )

        # 从数据库恢复数据
        context_data = db_context.get_context()
        if context_data:
            context.state = ConversationState(context_data.get("state", "init"))
            context.current_step = context_data.get("current_step", 0)
            context.source_type = context_data.get("source_type")
            context.source_group_id = context_data.get("source_group_id")
            context.source_keyword = context_data.get("source_keyword")
            context.tracking_code = context_data.get("tracking_code")
            context.steps_completed = context_data.get("steps_completed", [])

        # 恢复消息历史
        if db_context.message_history:
            try:
                context.message_history = json.loads(db_context.message_history)
            except json.JSONDecodeError:
                pass

        return context

    async def _save_to_db(self, context: ConversationData) -> None:
        """Save context to database."""
        from sqlalchemy import select

        result = await self.db.execute(
            select(DBConversationContext).where(
                DBConversationContext.user_id == context.user_id
            )
        )
        db_context = result.scalar_one_or_none()

        context_data = {
            "state": context.state.value,
            "current_step": context.current_step,
            "source_type": context.source_type,
            "source_group_id": context.source_group_id,
            "source_keyword": context.source_keyword,
            "tracking_code": context.tracking_code,
            "steps_completed": context.steps_completed,
        }

        if db_context:
            db_context.context_data = json.dumps(context_data, ensure_ascii=False)
            db_context.message_history = json.dumps(context.message_history, ensure_ascii=False)
            db_context.expires_at = datetime.utcnow() + timedelta(minutes=self.expire_minutes)
            db_context.updated_at = datetime.utcnow()
        else:
            db_context = DBConversationContext(
                user_id=context.user_id,
                context_data=json.dumps(context_data, ensure_ascii=False),
                message_history=json.dumps(context.message_history, ensure_ascii=False),
                expires_at=datetime.utcnow() + timedelta(minutes=self.expire_minutes),
            )
            self.db.add(db_context)

        await self.db.commit()
