"""
Tests for Reply Engine Module
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.ai.intent_classifier import IntentType
from app.modules.acquisition.auto_reply.reply_engine import ReplyContext, ReplyEngine
from app.modules.acquisition.constants import ResponseMode


class TestReplyEngine:
    @pytest.fixture
    def keyword_engine(self):
        engine = MagicMock()
        engine.match = AsyncMock()
        return engine

    @pytest.fixture
    def template_engine(self):
        engine = MagicMock()
        engine.get_template_by_keyword = AsyncMock()
        engine.render = MagicMock(return_value="template reply")
        return engine

    @pytest.fixture
    def llm_client(self):
        client = MagicMock()
        client.generate = AsyncMock(return_value="ai reply")
        return client

    @pytest.fixture
    def engine_with_ai(self, keyword_engine, template_engine, llm_client):
        engine = ReplyEngine(
            keyword_engine=keyword_engine,
            template_engine=template_engine,
            llm_client=llm_client,
        )
        engine._group_ai_settings = AsyncMock(
            return_value={
                "enabled": True,
                "aiEnabled": True,
                "allowKeywordTriggeredReply": True,
                "temperature": 0.6,
                "maxTokens": 180,
                "systemPrompt": "system",
            }
        )
        return engine

    @pytest.fixture
    def engine_without_ai(self, keyword_engine, template_engine):
        engine = ReplyEngine(
            keyword_engine=keyword_engine,
            template_engine=template_engine,
            llm_client=None,
        )
        engine._group_ai_settings = AsyncMock(
            return_value={
                "enabled": False,
                "aiEnabled": False,
                "allowKeywordTriggeredReply": False,
            }
        )
        return engine

    @pytest.mark.asyncio
    async def test_generate_reply_uses_ai_mode(self, engine_with_ai, keyword_engine):
        keyword_engine.match.return_value = [MagicMock(id=1, text="价格", keyword_type=MagicMock(value="price"))]
        context = ReplyContext(user_id=1, group_id=2)

        result = await engine_with_ai.generate_reply("价格多少", context)

        assert result.should_send is True
        assert result.mode == ResponseMode.PRIVATE
        assert result.content == "ai reply"

    @pytest.mark.asyncio
    async def test_generate_reply_falls_back_to_template_without_ai(self, engine_without_ai, keyword_engine, template_engine):
        keyword_engine.match.return_value = [MagicMock(id=1, text="怎么", keyword_type=MagicMock(value="inquiry"))]
        template_engine.get_template_by_keyword.return_value = None
        context = ReplyContext(user_id=1, group_id=2)

        result = await engine_without_ai.generate_reply("怎么用", context)

        assert result.mode == ResponseMode.GROUP
        assert result.should_send is True
        assert result.content != ""

    @pytest.mark.asyncio
    async def test_should_reply_false_when_no_match(self, engine_without_ai, keyword_engine):
        keyword_engine.match.return_value = []

        should_reply = await engine_without_ai.should_reply("hello")

        assert should_reply is False

    @pytest.mark.asyncio
    async def test_ai_generation_falls_back_on_error(self, keyword_engine, template_engine):
        llm_client = MagicMock()
        llm_client.generate = AsyncMock(side_effect=RuntimeError("boom"))
        engine = ReplyEngine(keyword_engine=keyword_engine, template_engine=template_engine, llm_client=llm_client)
        engine._group_ai_settings = AsyncMock(
            return_value={
                "enabled": True,
                "aiEnabled": True,
                "allowKeywordTriggeredReply": True,
                "temperature": 0.6,
                "maxTokens": 180,
                "systemPrompt": "system",
            }
        )

        keyword_engine.match.return_value = [MagicMock(id=1, text="买", keyword_type=MagicMock(value="demand"))]
        context = ReplyContext(user_id=1, group_id=2, intent=IntentType.DEMAND)

        result = await engine.generate_reply("我想买", context)

        assert result.should_send is True
        assert result.content != ""
        assert result.mode == ResponseMode.PRIVATE
