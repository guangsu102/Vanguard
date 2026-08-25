"""
Unit Tests for Guardian Rule Engine
"""

import pytest
from unittest.mock import AsyncMock

from app.modules.guardian.moderation.rule_engine import (
    GuardianRuleEngine,
    MatchedRule,
    EvaluationResult,
)
from app.modules.guardian.models import (
    RuleType,
    ViolationLevel,
    ViolationAction,
)


class TestGuardianRuleEngine:
    """Tests for GuardianRuleEngine."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database session."""
        db = AsyncMock()
        return db

    @pytest.fixture
    def mock_keyword_engine(self):
        """Create mock keyword engine."""
        engine = AsyncMock()
        engine.match = AsyncMock(return_value=[])
        engine.match_by_type = AsyncMock(return_value=[])
        engine.list_keywords = AsyncMock(return_value=[])
        return engine

    @pytest.fixture
    def rule_engine(self, mock_db, mock_keyword_engine):
        """Create rule engine instance."""
        return GuardianRuleEngine(mock_db, mock_keyword_engine)

    def test_evaluation_result_defaults(self):
        """Test EvaluationResult default values."""
        result = EvaluationResult(is_violation=False)
        
        assert result.is_violation is False
        assert result.matched_rules == []
        assert result.recommended_action == ViolationAction.WARN
        assert result.severity == ViolationLevel.LOW
        assert result.should_delete is False
        assert result.should_warn is True

    def test_matched_rule_creation(self):
        """Test MatchedRule creation."""
        matched = MatchedRule(
            rule_id=1,
            rule_type=RuleType.KEYWORD,
            pattern="test",
            matched_content="test",
            level=ViolationLevel.MEDIUM,
            action=ViolationAction.WARN
        )
        
        assert matched.rule_id == 1
        assert matched.rule_type == RuleType.KEYWORD
        assert matched.pattern == "test"
        assert matched.matched_content == "test"
        assert matched.level == ViolationLevel.MEDIUM
        assert matched.action == ViolationAction.WARN

    def test_evaluation_result_has_matched(self):
        """Test EvaluationResult.has_matched property."""
        result_empty = EvaluationResult(is_violation=False)
        assert result_empty.has_matched is False
        
        result_with_match = EvaluationResult(
            is_violation=True,
            matched_rules=[MatchedRule(
                rule_id=1,
                rule_type=RuleType.KEYWORD,
                pattern="test",
                matched_content="test",
                level=ViolationLevel.LOW,
                action=ViolationAction.WARN
            )]
        )
        assert result_with_match.has_matched is True

    def test_get_highest_severity(self, rule_engine):
        """Test severity calculation."""
        rules = [
            MatchedRule(1, RuleType.KEYWORD, "a", "a", ViolationLevel.LOW, ViolationAction.WARN),
            MatchedRule(2, RuleType.KEYWORD, "b", "b", ViolationLevel.HIGH, ViolationAction.BAN),
            MatchedRule(3, RuleType.KEYWORD, "c", "c", ViolationLevel.MEDIUM, ViolationAction.MUTE),
        ]
        
        highest = rule_engine._get_highest_severity(rules)
        assert highest == ViolationLevel.HIGH

    def test_get_highest_severity_empty(self, rule_engine):
        """Test severity calculation with empty list."""
        highest = rule_engine._get_highest_severity([])
        assert highest == ViolationLevel.LOW

    def test_get_recommended_action_high(self, rule_engine):
        """Test action recommendation for high severity."""
        rules = [MatchedRule(1, RuleType.KEYWORD, "test", "test", ViolationLevel.HIGH, ViolationAction.BAN)]
        action = rule_engine._get_recommended_action(rules, ViolationLevel.HIGH)
        assert action == ViolationAction.BAN

    def test_get_recommended_action_medium(self, rule_engine):
        """Test action recommendation for medium severity."""
        rules = [MatchedRule(1, RuleType.KEYWORD, "test", "test", ViolationLevel.MEDIUM, ViolationAction.MUTE)]
        action = rule_engine._get_recommended_action(rules, ViolationLevel.MEDIUM)
        assert action == ViolationAction.MUTE

    def test_get_recommended_action_low(self, rule_engine):
        """Test action recommendation for low severity."""
        rules = [MatchedRule(1, RuleType.KEYWORD, "test", "test", ViolationLevel.LOW, ViolationAction.WARN)]
        action = rule_engine._get_recommended_action(rules, ViolationLevel.LOW)
        assert action == ViolationAction.WARN

    def test_compile_pattern_keyword(self, rule_engine):
        """Test keyword pattern compilation."""
        pattern = rule_engine._compile_pattern("test", RuleType.KEYWORD)
        assert pattern is not None
        assert pattern.search("This is a test message") is not None
        assert pattern.search("No match here") is None

    def test_compile_pattern_domain(self, rule_engine):
        """Test domain pattern compilation."""
        pattern = rule_engine._compile_pattern(r"\.vip$", RuleType.DOMAIN)
        assert pattern is not None
        assert pattern.search("example.vip") is not None
        assert pattern.search("example.com") is None

    def test_compile_pattern_invalid(self, rule_engine):
        """Test invalid pattern handling."""
        pattern = rule_engine._compile_pattern("[invalid", RuleType.REGEX)
        assert pattern is None


class TestEvaluationResult:
    """Tests for EvaluationResult."""

    def test_default_values(self):
        """Test default values."""
        result = EvaluationResult(is_violation=False)
        
        assert result.is_violation is False
        assert result.matched_rules == []
        assert len(result.matched_rules) == 0

    def test_violation_result(self):
        """Test violation result."""
        result = EvaluationResult(
            is_violation=True,
            matched_rules=[
                MatchedRule(
                    rule_id=1,
                    rule_type=RuleType.KEYWORD,
                    pattern="bad",
                    matched_content="bad",
                    level=ViolationLevel.HIGH,
                    action=ViolationAction.BAN
                )
            ],
            recommended_action=ViolationAction.BAN,
            severity=ViolationLevel.HIGH,
            should_delete=True,
            should_warn=True
        )
        
        assert result.is_violation is True
        assert len(result.matched_rules) == 1
        assert result.has_matched is True
        assert result.severity == ViolationLevel.HIGH
        assert result.should_delete is True
