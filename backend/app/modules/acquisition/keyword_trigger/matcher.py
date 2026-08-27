"""
Keyword Matcher Module

Matches keywords in Telegram messages for trigger detection.
"""

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.keyword.engine import KeywordEngine, CompiledKeyword
from app.modules.acquisition.models import KeywordTrigger, TriggerRecord, TriggerType

logger = structlog.get_logger()


@dataclass
class TriggerMatch:
    """Represents a keyword trigger match."""
    trigger_id: int
    keyword_id: int
    keyword_text: str
    trigger_type: TriggerType
    matched_text: str
    match_position: int
    confidence: float = 1.0


class KeywordMatcher:
    """
    Keyword matcher for detecting trigger conditions in messages.

    Integrates with the core keyword engine to match messages
    against configured triggers.
    """

    def __init__(
        self,
        db: AsyncSession,
        keyword_engine: KeywordEngine,
    ):
        """
        Initialize KeywordMatcher.

        Args:
            db: Database session
            keyword_engine: Core keyword engine for matching
        """
        self.db = db
        self.keyword_engine = keyword_engine
        self._triggers: dict[int, KeywordTrigger] = {}
        self._lock = asyncio.Lock()
        self.logger = logger.bind(module="keyword_matcher")

    async def load_triggers(self) -> int:
        """
        Load all active triggers from database.

        Returns:
            Number of triggers loaded
        """
        from sqlalchemy import select

        async with self._lock:
            result = await self.db.execute(
                select(KeywordTrigger).where(
                    KeywordTrigger.enabled == True,
                    KeywordTrigger.requires_review == False,
                )
            )
            triggers = list(result.scalars().all())

            self._triggers.clear()
            for trigger in triggers:
                key = trigger.keyword_id if trigger.keyword_id is not None else -trigger.id
                self._triggers[key] = trigger

            self.logger.debug("triggers_loaded", count=len(self._triggers))
            return len(self._triggers)

    async def match(
        self,
        message_text: str,
        group_id: Optional[int] = None,
    ) -> list[TriggerMatch]:
        """
        Match message against configured triggers.

        Args:
            message_text: Text to match
            group_id: Optional group ID for group-specific triggers

        Returns:
            List of TriggerMatch objects
        """
        if not message_text:
            return []

        matches = []

        # 使用核心关键词引擎匹配
        keyword_matches = await self.keyword_engine.match(message_text)
        matched_trigger_ids: set[int] = set()

        for compiled in keyword_matches:
            # 查找对应的触发配置
            trigger = self._triggers.get(compiled.id)
            if not trigger:
                # 动态创建默认触发
                trigger = await self._get_or_create_trigger(compiled)

            if trigger and trigger.enabled:
                match = TriggerMatch(
                    trigger_id=trigger.id,
                    keyword_id=compiled.id,
                    keyword_text=compiled.text,
                    trigger_type=trigger.trigger_type,
                    matched_text=compiled.text,
                    match_position=self._find_match_position(message_text, compiled.text),
                    confidence=1.0,
                )
                matches.append(match)
                matched_trigger_ids.add(trigger.id)

                self.logger.debug(
                    "trigger_matched",
                    keyword_id=compiled.id,
                    trigger_id=trigger.id,
                    keyword=compiled.text,
                )

        for trigger in self._triggers.values():
            if not trigger.enabled or trigger.id in matched_trigger_ids:
                continue
            if not trigger.keyword_text:
                continue

            match_position = self._find_trigger_text_position(message_text, trigger.keyword_text)
            if match_position < 0:
                continue

            match = TriggerMatch(
                trigger_id=trigger.id,
                keyword_id=trigger.keyword_id or 0,
                keyword_text=trigger.keyword_text,
                trigger_type=trigger.trigger_type,
                matched_text=trigger.keyword_text,
                match_position=match_position,
                confidence=1.0,
            )
            matches.append(match)
            matched_trigger_ids.add(trigger.id)

            self.logger.debug(
                "text_trigger_matched",
                trigger_id=trigger.id,
                keyword=trigger.keyword_text,
            )

        return matches

    async def match_in_context(
        self,
        message_text: str,
        context: dict,
    ) -> list[TriggerMatch]:
        """
        Match with context awareness.

        Args:
            message_text: Message text
            context: Additional context (user history, group info, etc.)

        Returns:
            List of TriggerMatch with context consideration
        """
        matches = await self.match(message_text, context.get("group_id"))

        # 上下文过滤
        user_id = context.get("user_id")
        if user_id:
            # 检查用户触发历史
            matches = await self._filter_by_user_history(matches, user_id)

        return matches

    async def _get_or_create_trigger(
        self,
        compiled: CompiledKeyword,
    ) -> Optional[KeywordTrigger]:
        """Get or create default trigger for a keyword."""
        from sqlalchemy import select

        result = await self.db.execute(
            select(KeywordTrigger).where(KeywordTrigger.keyword_id == compiled.id)
        )
        trigger = result.scalar_one_or_none()

        if not trigger:
            # 创建默认触发配置
            trigger = KeywordTrigger(
                keyword_id=compiled.id,
                keyword_text=compiled.text,
                trigger_type=TriggerType.KEYWORD,
                action=KeywordTrigger.action if hasattr(KeywordTrigger, 'action') else None,
            )
            self.db.add(trigger)
            await self.db.commit()
            await self.db.refresh(trigger)

        return trigger

    async def _filter_by_user_history(
        self,
        matches: list[TriggerMatch],
        user_id: int,
    ) -> list[TriggerMatch]:
        """Filter matches based on user trigger history."""
        if not matches:
            return matches

        cooldown_map: dict[int, int] = {}
        for match in matches:
            trigger = await self.get_trigger_by_id(match.trigger_id)
            if trigger is None and match.keyword_id:
                trigger = await self.get_trigger_for_keyword(match.keyword_id)
            if trigger:
                cooldown_map[match.trigger_id] = max(trigger.cooldown_seconds, 0)

        if not cooldown_map:
            return matches

        from sqlalchemy import select

        deduped: list[TriggerMatch] = []
        now = datetime.utcnow()

        for match in matches:
            cooldown_seconds = cooldown_map.get(match.trigger_id, 0)
            if cooldown_seconds <= 0:
                deduped.append(match)
                continue

            cooldown_started_at = now - timedelta(seconds=cooldown_seconds)
            result = await self.db.execute(
                select(TriggerRecord.id)
                .where(TriggerRecord.user_id == user_id)
                .where(TriggerRecord.trigger_id == match.trigger_id)
                .where(TriggerRecord.created_at >= cooldown_started_at)
                .limit(1)
            )
            recent_record = result.scalar_one_or_none()
            if recent_record is not None:
                self.logger.debug(
                    "trigger_deduped_by_history",
                    user_id=user_id,
                    trigger_id=match.trigger_id,
                    keyword_id=match.keyword_id,
                )
                continue

            deduped.append(match)

        return deduped

    def _find_match_position(self, text: str, keyword: str) -> int:
        """Find the position of keyword match in text."""
        pos = text.lower().find(keyword.lower())
        return pos if pos >= 0 else 0

    def _find_trigger_text_position(self, text: str, keyword_text: str) -> int:
        """Find trigger text position using regex-like text when possible."""
        try:
            match = re.search(keyword_text, text, flags=re.IGNORECASE)
            if match:
                return match.start()
        except re.error:
            pass

        return text.lower().find(keyword_text.lower())

    async def get_trigger_for_keyword(
        self,
        keyword_id: int,
    ) -> Optional[KeywordTrigger]:
        """Get trigger configuration for a keyword."""
        return self._triggers.get(keyword_id)

    async def get_trigger_by_id(
        self,
        trigger_id: int,
    ) -> Optional[KeywordTrigger]:
        """Get trigger configuration by trigger ID."""
        for trigger in self._triggers.values():
            if trigger.id == trigger_id:
                return trigger
        return None

    async def update_trigger(
        self,
        trigger_id: int,
        **kwargs,
    ) -> Optional[KeywordTrigger]:
        """Update a trigger configuration."""
        from sqlalchemy import select

        result = await self.db.execute(
            select(KeywordTrigger).where(KeywordTrigger.id == trigger_id)
        )
        trigger = result.scalar_one_or_none()

        if not trigger:
            return None

        for key, value in kwargs.items():
            if hasattr(trigger, key):
                setattr(trigger, key, value)

        await self.db.commit()
        await self.load_triggers()  # 重新加载

        self.logger.info("trigger_updated", trigger_id=trigger_id)
        return trigger

    async def enable_trigger(self, trigger_id: int) -> bool:
        """Enable a trigger."""
        trigger = await self.update_trigger(trigger_id, enabled=True)
        return trigger is not None

    async def disable_trigger(self, trigger_id: int) -> bool:
        """Disable a trigger."""
        trigger = await self.update_trigger(trigger_id, enabled=False)
        return trigger is not None

    async def get_statistics(self) -> dict:
        """Get trigger statistics."""
        total_triggers = len(self._triggers)
        enabled_triggers = sum(1 for t in self._triggers.values() if t.enabled)

        return {
            "total_triggers": total_triggers,
            "enabled_triggers": enabled_triggers,
            "disabled_triggers": total_triggers - enabled_triggers,
        }
