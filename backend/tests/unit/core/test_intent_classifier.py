"""
Unit Tests for Intent Classifier Module

Tests cover:
- Intent classification (rule-based fallback)
- Response strategy mapping
- Keyword extraction
- Confidence scoring
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.ai.intent_classifier import (
    IntentClassifier,
    IntentType,
    IntentResult,
    ResponseStrategy,
)


class TestIntentClassifier:
    """Test IntentClassifier class."""

    @pytest.fixture
    def classifier(self):
        """Create IntentClassifier instance without LLM."""
        return IntentClassifier()

    def test_init_without_llm(self):
        """Test initialization without LLM client."""
        classifier = IntentClassifier()
        assert classifier.llm is None

    def test_init_with_llm(self):
        """Test initialization with LLM client."""
        mock_llm = MagicMock()
        classifier = IntentClassifier(llm_client=mock_llm)
        assert classifier.llm is mock_llm


class TestRuleBasedClassification:
    """Test rule-based classification fallback."""

    @pytest.fixture
    def classifier(self):
        """Create IntentClassifier instance."""
        return IntentClassifier(llm_client=None)

    def test_classify_demand(self, classifier):
        """Test demand intent classification."""
        messages = [
            "我想买一个套餐",
            "想要试试",
            "有套餐吗",
            "推荐一个好的",
            "想买机场",
        ]

        for msg in messages:
            result = classifier._rule_based_classify(msg)
            assert result.intent == IntentType.DEMAND
            assert result.confidence == 0.8

    def test_classify_inquiry(self, classifier):
        """Test inquiry intent classification."""
        messages = [
            "怎么用",
            "如何使用",
            "什么节点",
            "支持哪些功能",
            "能用吗",
        ]

        for msg in messages:
            result = classifier._rule_based_classify(msg)
            assert result.intent == IntentType.INQUIRY
            assert result.confidence == 0.7

    def test_classify_price(self, classifier):
        """Test price intent classification."""
        messages = [
            "收费多少",
            "收多少钱",
        ]

        for msg in messages:
            result = classifier._rule_based_classify(msg)
            assert result.intent == IntentType.PRICE, f"Failed for: {msg}, got {result.intent}"
            assert result.confidence == 0.9

    def test_classify_comparison(self, classifier):
        """Test comparison intent classification."""
        messages = [
            "这个比那个好",
            "做下对比",
            "两者的区别",
        ]

        for msg in messages:
            result = classifier._rule_based_classify(msg)
            assert result.intent == IntentType.COMPARISON, f"Failed for: {msg}, got {result.intent}"
            assert result.confidence == 0.8

    def test_classify_complaint(self, classifier):
        """Test complaint intent classification."""
        messages = [
            "服务不好",
            "垃圾",
            "要求退款",
            "我要投诉",
            "太差了",
        ]

        for msg in messages:
            result = classifier._rule_based_classify(msg)
            assert result.intent == IntentType.COMPLAINT
            assert result.confidence == 0.9

    def test_classify_chitchat(self, classifier):
        """Test chitchat intent classification."""
        messages = [
            "谢谢",
            "好的",
            "OK",
            "👋",
            "👍",
        ]

        for msg in messages:
            result = classifier._rule_based_classify(msg)
            assert result.intent == IntentType.CHITCHAT
            assert result.confidence == 0.8

    def test_classify_other(self, classifier):
        """Test other intent classification."""
        result = classifier._rule_based_classify("今天天气真好")
        assert result.intent == IntentType.OTHER
        assert result.confidence == 0.5

    def test_classify_case_insensitive(self, classifier):
        """Test classification is case insensitive."""
        result_lower = classifier._rule_based_classify("多少钱")
        result_upper = classifier._rule_based_classify("多少钱".upper())
        result_mixed = classifier._rule_based_classify("多少钱".title())

        assert result_lower.intent == result_upper.intent == result_mixed.intent


class TestClassify:
    """Test classify method with async support."""

    @pytest.fixture
    def classifier(self):
        """Create IntentClassifier instance."""
        return IntentClassifier()

    @pytest.mark.asyncio
    async def test_classify_without_llm_uses_rule_based(self, classifier):
        """Test classify falls back to rule-based without LLM."""
        result = await classifier.classify("多少钱")

        assert result.intent == IntentType.PRICE
        assert result.confidence == 0.9

    @pytest.mark.asyncio
    async def test_classify_with_llm_success(self):
        """Test classify with LLM client."""
        mock_llm = MagicMock()
        mock_llm.generate = AsyncMock(
            return_value='{"intent": "demand", "confidence": 0.95, "reason": "购买意向", "keywords": ["买", "套餐"]}'
        )
        classifier = IntentClassifier(llm_client=mock_llm)

        result = await classifier.classify("想买套餐")

        assert result.intent == IntentType.DEMAND
        assert result.confidence == 0.95
        assert result.reason == "购买意向"
        mock_llm.generate.assert_called_once()

    @pytest.mark.asyncio
    async def test_classify_with_llm_error_fallback(self):
        """Test classify falls back to rule-based on LLM error."""
        mock_llm = MagicMock()
        mock_llm.generate = AsyncMock(side_effect=Exception("LLM error"))
        classifier = IntentClassifier(llm_client=mock_llm)

        result = await classifier.classify("多少钱")

        assert result.intent == IntentType.PRICE


class TestKeywordExtraction:
    """Test keyword extraction."""

    @pytest.fixture
    def classifier(self):
        """Create IntentClassifier instance."""
        return IntentClassifier()

    def test_extract_keywords(self, classifier):
        """Test keyword extraction."""
        text = "我想买机场的套餐，想要好节点"

        keywords = classifier._extract_keywords(text)

        assert "机场" in keywords
        assert "节点" in keywords
        assert "套餐" in keywords

    def test_extract_keywords_limit(self, classifier):
        """Test keyword extraction limited to 3."""
        text = "机场节点套餐流量速度价格"

        keywords = classifier._extract_keywords(text)

        assert len(keywords) <= 3

    def test_extract_keywords_empty(self, classifier):
        """Test keyword extraction with no matches."""
        text = "今天天气很好"

        keywords = classifier._extract_keywords(text)

        assert len(keywords) == 0

    def test_extract_keywords_with_vpn(self, classifier):
        """Test keyword extraction includes VPN terms."""
        text = "VPN梯子推荐"

        keywords = classifier._extract_keywords(text)

        assert "VPN" in keywords
        assert "梯子" in keywords


class TestResponseStrategy:
    """Test response strategy mapping."""

    @pytest.fixture
    def classifier(self):
        """Create IntentClassifier instance."""
        return IntentClassifier()

    def test_strategy_demand(self, classifier):
        """Test demand intent strategy."""
        strategy = classifier.get_strategy(IntentType.DEMAND)

        assert strategy.action == "private_chat"
        assert strategy.priority == "high"
        assert strategy.include_coupon is True
        assert strategy.template == "邀请注册"

    def test_strategy_inquiry(self, classifier):
        """Test inquiry intent strategy."""
        strategy = classifier.get_strategy(IntentType.INQUIRY)

        assert strategy.action == "group_reply"
        assert strategy.priority == "medium"
        assert strategy.include_link is True

    def test_strategy_price(self, classifier):
        """Test price intent strategy."""
        strategy = classifier.get_strategy(IntentType.PRICE)

        assert strategy.action == "private_chat"
        assert strategy.priority == "high"
        assert strategy.include_coupon is True

    def test_strategy_comparison(self, classifier):
        """Test comparison intent strategy."""
        strategy = classifier.get_strategy(IntentType.COMPARISON)

        assert strategy.action == "group_reply"
        assert strategy.priority == "medium"
        assert strategy.include_link is True

    def test_strategy_complaint(self, classifier):
        """Test complaint intent strategy."""
        strategy = classifier.get_strategy(IntentType.COMPLAINT)

        assert strategy.action == "escalate"
        assert strategy.priority == "high"
        assert strategy.notify_admin is True

    def test_strategy_chitchat(self, classifier):
        """Test chitchat intent strategy."""
        strategy = classifier.get_strategy(IntentType.CHITCHAT)

        assert strategy.action == "ignore"
        assert strategy.priority == "low"

    def test_strategy_other(self, classifier):
        """Test other intent strategy."""
        strategy = classifier.get_strategy(IntentType.OTHER)

        assert strategy.action == "keyword_fallback"
        assert strategy.priority == "medium"


class TestClassifyAndGetStrategy:
    """Test classify_and_get_strategy method."""

    @pytest.fixture
    def classifier(self):
        """Create IntentClassifier instance."""
        return IntentClassifier(llm_client=None)

    @pytest.mark.asyncio
    async def test_classify_and_get_strategy(self, classifier):
        """Test combined classification and strategy retrieval."""
        intent_result, strategy = await classifier.classify_and_get_strategy("多少钱")

        assert intent_result.intent == IntentType.PRICE
        assert strategy.action == "private_chat"
        assert strategy.priority == "high"


class TestIntentResult:
    """Test IntentResult dataclass."""

    def test_create_intent_result(self):
        """Test creating IntentResult."""
        result = IntentResult(
            intent=IntentType.DEMAND,
            confidence=0.9,
            reason="购买意向",
            keywords=["买", "套餐"],
        )

        assert result.intent == IntentType.DEMAND
        assert result.confidence == 0.9
        assert result.reason == "购买意向"
        assert result.keywords == ["买", "套餐"]


class TestResponseStrategyDataclass:
    """Test ResponseStrategy dataclass."""

    def test_create_response_strategy(self):
        """Test creating ResponseStrategy."""
        strategy = ResponseStrategy(
            action="group_reply",
            priority="high",
            template="价格说明",
            include_coupon=True,
            include_link=False,
            notify_admin=True,
        )

        assert strategy.action == "group_reply"
        assert strategy.priority == "high"
        assert strategy.template == "价格说明"
        assert strategy.include_coupon is True
        assert strategy.include_link is False
        assert strategy.notify_admin is True

    def test_response_strategy_defaults(self):
        """Test ResponseStrategy default values."""
        strategy = ResponseStrategy(
            action="ignore",
            priority="low",
        )

        assert strategy.template is None
        assert strategy.include_coupon is False
        assert strategy.include_link is False
        assert strategy.notify_admin is False


class TestIntentTypeEnum:
    """Test IntentType enum."""

    def test_all_intent_types_defined(self):
        """Test all expected intent types are defined."""
        expected_types = {
            IntentType.DEMAND,
            IntentType.INQUIRY,
            IntentType.PRICE,
            IntentType.COMPARISON,
            IntentType.COMPLAINT,
            IntentType.CHITCHAT,
            IntentType.OTHER,
        }

        assert set(IntentType) == expected_types

    def test_intent_type_values(self):
        """Test intent type string values."""
        assert IntentType.DEMAND.value == "demand"
        assert IntentType.INQUIRY.value == "inquiry"
        assert IntentType.PRICE.value == "price"
        assert IntentType.COMPLAINT.value == "complaint"
