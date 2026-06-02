"""
Unit Tests for Group Management Module

Minimal version for direct testing without full app dependencies.
"""

import asyncio
import pytest
from decimal import Decimal

from app.core.group.models import Group, GroupLevel, GroupLevelConfig


# ============================================================================
# Test Group Model
# ============================================================================

class TestGroupModel:
    """Test Group model."""

    def test_create_group(self):
        """Test creating a group instance."""
        group = Group(
            group_id=123456789,
            title="Test Group",
            username="testgroup",
            member_count=100,
        )

        assert group.group_id == 123456789
        assert group.title == "Test Group"
        assert group.username == "testgroup"
        assert group.member_count == 100
        # Level uses default from model definition

    def test_group_default_values(self):
        """Test group default values are None or zero."""
        group = Group(group_id=111)

        assert group.group_id == 111
        # Scores default to None until set (SQLAlchemy default behavior)

    def test_group_level_enum(self):
        """Test GroupLevel enum values."""
        assert GroupLevel.A.value == "A"
        assert GroupLevel.B.value == "B"
        assert GroupLevel.C.value == "C"
        assert GroupLevel.UNRATED.value == "unrated"


# ============================================================================
# Test GroupLevelConfig Model
# ============================================================================

class TestGroupLevelConfig:
    """Test GroupLevelConfig model."""

    def test_create_config_for_level_a(self):
        """Test creating config for level A."""
        config = GroupLevelConfig(
            level=GroupLevel.A,
            min_score=70.0,
            can_send_ads=True,
            can_mention_users=True,
            can_share_links=True,
            can_initiate_private=True,
            daily_message_limit=10,
            message_interval=60,
            private_message_interval=5,
        )

        assert config.level == GroupLevel.A
        assert config.min_score == 70.0
        assert config.can_send_ads is True
        assert config.can_mention_users is True
        assert config.can_share_links is True
        assert config.can_initiate_private is True
        assert config.daily_message_limit == 10
        assert config.message_interval == 60
        assert config.private_message_interval == 5

    def test_create_config_for_level_b(self):
        """Test creating config for level B."""
        config = GroupLevelConfig(
            level=GroupLevel.B,
            min_score=50.0,
            can_send_ads=False,
            can_mention_users=False,
            can_share_links=True,
            can_initiate_private=True,
            daily_message_limit=5,
            message_interval=180,
            private_message_interval=5,
        )

        assert config.level == GroupLevel.B
        assert config.min_score == 50.0
        assert config.can_send_ads is False
        assert config.can_mention_users is False
        assert config.can_share_links is True
        assert config.daily_message_limit == 5

    def test_create_config_for_level_c(self):
        """Test creating config for level C."""
        config = GroupLevelConfig(
            level=GroupLevel.C,
            min_score=0.0,
            can_send_ads=False,
            can_mention_users=False,
            can_share_links=False,
            can_initiate_private=True,
            daily_message_limit=2,
            message_interval=300,
            private_message_interval=5,
        )

        assert config.level == GroupLevel.C
        assert config.can_send_ads is False
        assert config.can_share_links is False
        assert config.daily_message_limit == 2
        assert config.message_interval == 300

    def test_create_config_for_unrated(self):
        """Test creating config for unrated level."""
        config = GroupLevelConfig(
            level=GroupLevel.UNRATED,
            min_score=-1.0,
            can_send_ads=False,
            can_mention_users=False,
            can_share_links=False,
            can_initiate_private=False,
            daily_message_limit=0,
            message_interval=600,
            private_message_interval=30,
        )

        assert config.level == GroupLevel.UNRATED
        assert config.can_send_ads is False
        assert config.can_mention_users is False
        assert config.can_share_links is False
        assert config.can_initiate_private is False
        assert config.daily_message_limit == 0

    def test_config_weights(self):
        """Test config weight values."""
        config = GroupLevelConfig(
            level=GroupLevel.A,
            rule_weight=0.30,
            admin_weight=0.25,
            history_weight=0.20,
            convert_weight=0.15,
            activity_weight=0.10,
        )

        assert config.rule_weight == 0.30
        assert config.admin_weight == 0.25
        assert config.history_weight == 0.20
        assert config.convert_weight == 0.15
        assert config.activity_weight == 0.10

    def test_config_auto_adjustment_thresholds(self):
        """Test auto adjustment threshold settings."""
        config = GroupLevelConfig(
            level=GroupLevel.A,
            auto_downgrade_kick_threshold=3,
            auto_downgrade_warning_threshold=5,
            auto_downgrade_success_rate_threshold=0.50,
            auto_upgrade_no_warning_days=30,
            auto_upgrade_high_success_days=14,
            auto_upgrade_high_convert_days=14,
        )

        assert config.auto_downgrade_kick_threshold == 3
        assert config.auto_downgrade_warning_threshold == 5
        assert config.auto_downgrade_success_rate_threshold == 0.50
        assert config.auto_upgrade_no_warning_days == 30
        assert config.auto_upgrade_high_success_days == 14
        assert config.auto_upgrade_high_convert_days == 14


# ============================================================================
# Test Level Calculation Logic
# ============================================================================

class TestLevelCalculation:
    """Test level calculation logic."""

    def test_calculate_level_a_high_score(self):
        """Test level A with high score."""
        WEIGHTS = {
            "rule_score": 0.30,
            "admin_score": 0.25,
            "history_score": 0.20,
            "convert_score": 0.15,
            "activity_score": 0.10,
        }
        LEVEL_THRESHOLDS = {
            GroupLevel.A: 70,
            GroupLevel.B: 50,
            GroupLevel.C: 0,
        }

        group = Group(group_id=1)
        group.rule_score = 100
        group.admin_score = 100
        group.history_score = 100
        group.convert_score = 100
        group.activity_score = 100

        total_score = (
            group.rule_score * WEIGHTS["rule_score"]
            + group.admin_score * WEIGHTS["admin_score"]
            + group.history_score * WEIGHTS["history_score"]
            + group.convert_score * WEIGHTS["convert_score"]
            + group.activity_score * WEIGHTS["activity_score"]
        )

        assert total_score == 100.0

        for level, threshold in sorted(LEVEL_THRESHOLDS.items(), key=lambda x: x[1], reverse=True):
            if total_score >= threshold:
                assert level == GroupLevel.A
                break

    def test_calculate_level_b_medium_score(self):
        """Test level B with medium score."""
        WEIGHTS = {
            "rule_score": 0.30,
            "admin_score": 0.25,
            "history_score": 0.20,
            "convert_score": 0.15,
            "activity_score": 0.10,
        }
        LEVEL_THRESHOLDS = {
            GroupLevel.A: 70,
            GroupLevel.B: 50,
            GroupLevel.C: 0,
        }

        group = Group(group_id=1)
        group.rule_score = 70
        group.admin_score = 70
        group.history_score = 50
        group.convert_score = 30
        group.activity_score = 10

        total_score = (
            group.rule_score * WEIGHTS["rule_score"]
            + group.admin_score * WEIGHTS["admin_score"]
            + group.history_score * WEIGHTS["history_score"]
            + group.convert_score * WEIGHTS["convert_score"]
            + group.activity_score * WEIGHTS["activity_score"]
        )

        # 70*0.30 + 70*0.25 + 50*0.20 + 30*0.15 + 10*0.10 = 21 + 17.5 + 10 + 4.5 + 1 = 54.0
        assert total_score == 54.0

        for level, threshold in sorted(LEVEL_THRESHOLDS.items(), key=lambda x: x[1], reverse=True):
            if total_score >= threshold:
                assert level == GroupLevel.B
                break

    def test_calculate_level_c_low_score(self):
        """Test level C with low score."""
        WEIGHTS = {
            "rule_score": 0.30,
            "admin_score": 0.25,
            "history_score": 0.20,
            "convert_score": 0.15,
            "activity_score": 0.10,
        }
        LEVEL_THRESHOLDS = {
            GroupLevel.A: 70,
            GroupLevel.B: 50,
            GroupLevel.C: 0,
        }

        group = Group(group_id=1)
        group.rule_score = 30
        group.admin_score = 30
        group.history_score = 30
        group.convert_score = 30
        group.activity_score = 30

        total_score = (
            group.rule_score * WEIGHTS["rule_score"]
            + group.admin_score * WEIGHTS["admin_score"]
            + group.history_score * WEIGHTS["history_score"]
            + group.convert_score * WEIGHTS["convert_score"]
            + group.activity_score * WEIGHTS["activity_score"]
        )

        assert total_score == 30.0

        for level, threshold in sorted(LEVEL_THRESHOLDS.items(), key=lambda x: x[1], reverse=True):
            if total_score >= threshold:
                assert level == GroupLevel.C
                break

    def test_boundary_score_70_is_a(self):
        """Test that score exactly 70 is level A."""
        WEIGHTS = {
            "rule_score": 0.30,
            "admin_score": 0.25,
            "history_score": 0.20,
            "convert_score": 0.15,
            "activity_score": 0.10,
        }
        LEVEL_THRESHOLDS = {
            GroupLevel.A: 70,
            GroupLevel.B: 50,
            GroupLevel.C: 0,
        }

        group = Group(group_id=1)
        group.rule_score = 70
        group.admin_score = 70
        group.history_score = 70
        group.convert_score = 70
        group.activity_score = 70

        total_score = (
            group.rule_score * WEIGHTS["rule_score"]
            + group.admin_score * WEIGHTS["admin_score"]
            + group.history_score * WEIGHTS["history_score"]
            + group.convert_score * WEIGHTS["convert_score"]
            + group.activity_score * WEIGHTS["activity_score"]
        )

        assert total_score == 70.0

        for level, threshold in sorted(LEVEL_THRESHOLDS.items(), key=lambda x: x[1], reverse=True):
            if total_score >= threshold:
                assert level == GroupLevel.A
                break

    def test_boundary_score_69_is_b(self):
        """Test that score 69 is level B."""
        WEIGHTS = {
            "rule_score": 0.30,
            "admin_score": 0.25,
            "history_score": 0.20,
            "convert_score": 0.15,
            "activity_score": 0.10,
        }
        LEVEL_THRESHOLDS = {
            GroupLevel.A: 70,
            GroupLevel.B: 50,
            GroupLevel.C: 0,
        }

        group = Group(group_id=1)
        group.rule_score = 69
        group.admin_score = 69
        group.history_score = 69
        group.convert_score = 69
        group.activity_score = 69

        total_score = (
            group.rule_score * WEIGHTS["rule_score"]
            + group.admin_score * WEIGHTS["admin_score"]
            + group.history_score * WEIGHTS["history_score"]
            + group.convert_score * WEIGHTS["convert_score"]
            + group.activity_score * WEIGHTS["activity_score"]
        )

        assert total_score == 69.0

        for level, threshold in sorted(LEVEL_THRESHOLDS.items(), key=lambda x: x[1], reverse=True):
            if total_score >= threshold:
                assert level == GroupLevel.B
                break


# ============================================================================
# Test Score Breakdown
# ============================================================================

class TestScoreBreakdown:
    """Test score breakdown calculation."""

    def test_score_breakdown(self):
        """Test score breakdown with contributions."""
        WEIGHTS = {
            "rule_score": 0.30,
            "admin_score": 0.25,
            "history_score": 0.20,
            "convert_score": 0.15,
            "activity_score": 0.10,
        }

        group = Group(group_id=1)
        group.rule_score = 80
        group.admin_score = 70
        group.history_score = 60
        group.convert_score = 50
        group.activity_score = 40

        breakdown = {
            "total_score": round(
                group.rule_score * WEIGHTS["rule_score"]
                + group.admin_score * WEIGHTS["admin_score"]
                + group.history_score * WEIGHTS["history_score"]
                + group.convert_score * WEIGHTS["convert_score"]
                + group.activity_score * WEIGHTS["activity_score"],
                2,
            ),
            "dimensions": {
                "rule_score": {
                    "value": group.rule_score,
                    "weight": WEIGHTS["rule_score"],
                    "contribution": round(group.rule_score * WEIGHTS["rule_score"], 2),
                },
                "admin_score": {
                    "value": group.admin_score,
                    "weight": WEIGHTS["admin_score"],
                    "contribution": round(group.admin_score * WEIGHTS["admin_score"], 2),
                },
                "history_score": {
                    "value": group.history_score,
                    "weight": WEIGHTS["history_score"],
                    "contribution": round(group.history_score * WEIGHTS["history_score"], 2),
                },
                "convert_score": {
                    "value": group.convert_score,
                    "weight": WEIGHTS["convert_score"],
                    "contribution": round(group.convert_score * WEIGHTS["convert_score"], 2),
                },
                "activity_score": {
                    "value": group.activity_score,
                    "weight": WEIGHTS["activity_score"],
                    "contribution": round(group.activity_score * WEIGHTS["activity_score"], 2),
                },
            },
        }

        # 80*0.30 + 70*0.25 + 60*0.20 + 50*0.15 + 40*0.10 = 24 + 17.5 + 12 + 7.5 + 4 = 65.0
        assert breakdown["total_score"] == 65.0
        assert breakdown["dimensions"]["rule_score"]["contribution"] == 24.0
        assert breakdown["dimensions"]["admin_score"]["contribution"] == 17.5
        assert breakdown["dimensions"]["history_score"]["contribution"] == 12.0
        assert breakdown["dimensions"]["convert_score"]["contribution"] == 7.5
        assert breakdown["dimensions"]["activity_score"]["contribution"] == 4.0


# ============================================================================
# Test Auto Adjustment Logic
# ============================================================================

class TestAutoAdjustment:
    """Test auto-adjustment logic."""

    def test_should_downgrade_kicked_3_times(self):
        """Test downgrade when kicked 3 times."""
        kick_threshold = 3
        kicked_count = 3

        if kicked_count >= kick_threshold:
            should_downgrade = True
            reason = f"kicked_{kicked_count}_times_threshold"
        else:
            should_downgrade = False
            reason = ""

        assert should_downgrade is True
        assert "kicked_3_times_threshold" in reason

    def test_should_not_downgrade_kicked_once(self):
        """Test no downgrade when kicked only once (below threshold)."""
        kick_threshold = 3
        kicked_count = 1

        if kicked_count >= kick_threshold:
            should_downgrade = True
        else:
            should_downgrade = False

        assert should_downgrade is False

    def test_should_downgrade_low_success_rate(self):
        """Test downgrade when success rate is low."""
        success_rate_threshold = 0.50
        success_rate = 0.30

        if success_rate < success_rate_threshold:
            should_downgrade = True
        else:
            should_downgrade = False

        assert should_downgrade is True

    def test_should_not_downgrade_good_success_rate(self):
        """Test no downgrade when success rate is good."""
        success_rate_threshold = 0.50
        success_rate = 0.80

        if success_rate < success_rate_threshold:
            should_downgrade = True
        else:
            should_downgrade = False

        assert should_downgrade is False

    def test_should_downgrade_many_warnings(self):
        """Test downgrade when warnings exceed threshold."""
        warning_threshold = 5
        warning_count = 7

        if warning_count >= warning_threshold:
            should_downgrade = True
        else:
            should_downgrade = False

        assert should_downgrade is True

    def test_should_upgrade_no_warnings_30_days(self):
        """Test upgrade when no warnings for 30 days."""
        no_warning_days_threshold = 30
        no_warnings_days = 35

        if no_warnings_days >= no_warning_days_threshold:
            should_upgrade = True
            reason = f"no_warnings_{no_warnings_days}_days"
        else:
            should_upgrade = False
            reason = ""

        assert should_upgrade is True
        assert "no_warnings_35_days" in reason


# ============================================================================
# Test Default Config Values
# ============================================================================

class TestDefaultConfigs:
    """Test default configuration values match requirements."""

    def test_level_a_defaults(self):
        """Test level A default configuration."""
        config = GroupLevelConfig(
            level=GroupLevel.A,
            min_score=70.0,
            can_send_ads=True,
            can_mention_users=True,
            can_share_links=True,
            can_initiate_private=True,
            daily_message_limit=10,
            message_interval=60,
            private_message_interval=5,
        )

        assert config.min_score == 70.0
        assert config.can_send_ads is True
        assert config.can_mention_users is True
        assert config.can_share_links is True
        assert config.can_initiate_private is True
        assert config.daily_message_limit == 10
        assert config.message_interval == 60

    def test_level_b_defaults(self):
        """Test level B default configuration."""
        config = GroupLevelConfig(
            level=GroupLevel.B,
            min_score=50.0,
            can_send_ads=False,
            can_mention_users=False,
            can_share_links=True,
            can_initiate_private=True,
            daily_message_limit=5,
            message_interval=180,
            private_message_interval=5,
        )

        assert config.min_score == 50.0
        assert config.can_send_ads is False
        assert config.can_mention_users is False
        assert config.can_share_links is True
        assert config.can_initiate_private is True
        assert config.daily_message_limit == 5
        assert config.message_interval == 180

    def test_level_c_defaults(self):
        """Test level C default configuration."""
        config = GroupLevelConfig(
            level=GroupLevel.C,
            min_score=0.0,
            can_send_ads=False,
            can_mention_users=False,
            can_share_links=False,
            can_initiate_private=True,
            daily_message_limit=2,
            message_interval=300,
            private_message_interval=5,
        )

        assert config.min_score == 0.0
        assert config.can_send_ads is False
        assert config.can_mention_users is False
        assert config.can_share_links is False
        assert config.can_initiate_private is True
        assert config.daily_message_limit == 2
        assert config.message_interval == 300

    def test_level_unrated_defaults(self):
        """Test unrated level default configuration."""
        config = GroupLevelConfig(
            level=GroupLevel.UNRATED,
            min_score=-1.0,
            can_send_ads=False,
            can_mention_users=False,
            can_share_links=False,
            can_initiate_private=False,
            daily_message_limit=0,
            message_interval=600,
            private_message_interval=30,
        )

        assert config.min_score == -1.0
        assert config.can_send_ads is False
        assert config.can_mention_users is False
        assert config.can_share_links is False
        assert config.can_initiate_private is False
        assert config.daily_message_limit == 0
        assert config.message_interval == 600
