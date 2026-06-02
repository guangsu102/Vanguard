"""
Group Scorer Module

Calculates and manages group scores based on multiple dimensions.

Scoring dimensions:
- Rule score (30%): Group rules and control
- Admin score (25%): Admin attitude
- History score (20%): Bot history in the group
- Convert score (15%): Conversion effectiveness
- Activity score (10%): Group activity level

All thresholds are configurable via GroupLevelConfig.
"""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from app.core.group.models import Group, GroupLevel

if TYPE_CHECKING:
    from app.core.group.manager import GroupManager


class GroupScorer:
    """
    Group scoring calculator.

    Calculates overall group score based on multiple weighted dimensions
    and determines group level (A/B/C) based on the score.
    Weights and thresholds are loaded from database configuration.
    """

    def __init__(self, manager: "GroupManager"):
        """
        Initialize GroupScorer with manager reference.

        Args:
            manager: GroupManager instance for accessing configs
        """
        self.manager = manager

    async def _get_weights(self) -> dict[str, float]:
        """Get scoring weights from config."""
        await self.manager._load_config_cache()
        weights = {}
        for level, config in self.manager._config_cache.items():
            weights = {
                "rule_score": config.get("rule_weight", 0.30),
                "admin_score": config.get("admin_weight", 0.25),
                "history_score": config.get("history_weight", 0.20),
                "convert_score": config.get("convert_weight", 0.15),
                "activity_score": config.get("activity_weight", 0.10),
            }
            break
        if not weights:
            weights = {
                "rule_score": 0.30,
                "admin_score": 0.25,
                "history_score": 0.20,
                "convert_score": 0.15,
                "activity_score": 0.10,
            }
        return weights

    async def _get_thresholds(self) -> dict[GroupLevel, float]:
        """Get level thresholds from config."""
        await self.manager._load_config_cache()
        thresholds = {}
        for level, config in self.manager._config_cache.items():
            min_score = config.get("min_score", 0.0)
            if level == GroupLevel.A:
                thresholds[GroupLevel.A] = min_score if min_score > 0 else 70.0
            elif level == GroupLevel.B:
                thresholds[GroupLevel.B] = min_score if min_score > 0 else 50.0
            elif level == GroupLevel.C:
                thresholds[GroupLevel.C] = 0.0

        if not thresholds:
            thresholds = {
                GroupLevel.A: 70.0,
                GroupLevel.B: 50.0,
                GroupLevel.C: 0.0,
            }
        return thresholds

    async def calculate_level(self, group: Group) -> GroupLevel:
        """
        Calculate group level based on weighted scores.

        Args:
            group: Group instance with scores

        Returns:
            Calculated GroupLevel
        """
        total_score = await self.calculate_total_score(group)
        thresholds = await self._get_thresholds()

        for level, threshold in sorted(
            thresholds.items(),
            key=lambda x: x[1],
            reverse=True,
        ):
            if total_score >= threshold:
                return level

        return GroupLevel.C

    async def calculate_total_score(self, group: Group) -> float:
        """
        Calculate weighted total score.

        Args:
            group: Group instance

        Returns:
            Weighted total score (0-100)
        """
        weights = await self._get_weights()

        return (
            group.rule_score * weights["rule_score"]
            + group.admin_score * weights["admin_score"]
            + group.history_score * weights["history_score"]
            + group.convert_score * weights["convert_score"]
            + group.activity_score * weights["activity_score"]
        )

    def update_score(
        self,
        group: Group,
        dimension: str,
        delta: int,
        max_score: int = 100,
    ) -> float:
        """
        Update a specific score dimension.

        Args:
            group: Group instance
            dimension: Score dimension name
            delta: Change in score (-100 to +100)
            max_score: Maximum score for this dimension

        Returns:
            New total score (synchronous, weights not recalculated)
        """
        current = getattr(group, f"{dimension}_score", 0)
        new_value = max(0, min(max_score, current + delta))
        setattr(group, f"{dimension}_score", new_value)
        return (
            group.rule_score * 0.30
            + group.admin_score * 0.25
            + group.history_score * 0.20
            + group.convert_score * 0.15
            + group.activity_score * 0.10
        )

    async def get_score_breakdown(self, group: Group) -> dict:
        """
        Get detailed score breakdown.

        Args:
            group: Group instance

        Returns:
            Dictionary with score breakdown
        """
        weights = await self._get_weights()
        total = await self.calculate_total_score(group)

        breakdown = {
            "total_score": round(total, 2),
            "dimensions": {
                "rule_score": {
                    "value": group.rule_score,
                    "weight": weights["rule_score"],
                    "contribution": round(
                        group.rule_score * weights["rule_score"], 2
                    ),
                },
                "admin_score": {
                    "value": group.admin_score,
                    "weight": weights["admin_score"],
                    "contribution": round(
                        group.admin_score * weights["admin_score"], 2
                    ),
                },
                "history_score": {
                    "value": group.history_score,
                    "weight": weights["history_score"],
                    "contribution": round(
                        group.history_score * weights["history_score"], 2
                    ),
                },
                "convert_score": {
                    "value": group.convert_score,
                    "weight": weights["convert_score"],
                    "contribution": round(
                        group.convert_score * weights["convert_score"], 2
                    ),
                },
                "activity_score": {
                    "value": group.activity_score,
                    "weight": weights["activity_score"],
                    "contribution": round(
                        group.activity_score * weights["activity_score"], 2
                    ),
                },
            },
            "level": (await self.calculate_level(group)).value,
        }

        return breakdown

    async def _get_auto_adjustment_config(self, level: GroupLevel) -> dict:
        """Get auto adjustment thresholds for a level from config."""
        await self.manager._load_config_cache()
        config = self.manager._get_config(level)
        return {
            "kick_threshold": config.get("auto_downgrade_kick_threshold", 3),
            "warning_threshold": config.get("auto_downgrade_warning_threshold", 5),
            "success_rate_threshold": config.get("auto_downgrade_success_rate_threshold", 0.50),
            "no_warning_days": config.get("auto_upgrade_no_warning_days", 30),
            "high_success_days": config.get("auto_upgrade_high_success_days", 14),
            "high_convert_days": config.get("auto_upgrade_high_convert_days", 14),
        }

    async def should_auto_downgrade(self, group: Group, events: dict) -> tuple[bool, str]:
        """
        Check if group should be auto-downgraded based on recent events.

        Args:
            group: Group instance
            events: Dictionary with recent events:
                - kicked_count: Times bot was kicked in last 7 days
                - warning_count: Warnings received in last 7 days
                - success_rate: Message success rate in last 7 days

        Returns:
            Tuple of (should_downgrade, reason)
        """
        config = await self._get_auto_adjustment_config(group.level)

        kicked_count = events.get("kicked_count", 0)
        warning_count = events.get("warning_count", 0)
        success_rate = events.get("success_rate", 1.0)

        if kicked_count >= config["kick_threshold"]:
            return True, f"kicked_{kicked_count}_times_threshold"
        elif kicked_count >= 1:
            return True, f"kicked_1_time"

        if success_rate < config["success_rate_threshold"]:
            return True, f"low_success_rate_{success_rate:.0%}"

        if warning_count >= config["warning_threshold"]:
            return True, f"many_warnings_{warning_count}"

        return False, ""

    async def should_auto_upgrade(self, group: Group, events: dict) -> tuple[bool, str]:
        """
        Check if group should be auto-upgraded based on recent events.

        Args:
            group: Group instance
            events: Dictionary with recent events:
                - no_warnings_days: Consecutive days without warnings
                - high_success_rate_days: Days with success rate > 90%
                - high_conversion_days: Days with conversion rate > 5%

        Returns:
            Tuple of (should_upgrade, reason)
        """
        config = await self._get_auto_adjustment_config(group.level)

        no_warnings_days = events.get("no_warnings_days", 0)
        high_success_rate_days = events.get("high_success_rate_days", 0)
        high_conversion_days = events.get("high_conversion_days", 0)

        if no_warnings_days >= config["no_warning_days"]:
            return True, f"no_warnings_{no_warnings_days}_days"

        if high_success_rate_days >= config["high_success_days"] and group.level != GroupLevel.A:
            return True, f"high_success_{high_success_rate_days}_days"

        if high_conversion_days >= config["high_convert_days"] and group.level != GroupLevel.A:
            return True, f"high_conversion_{high_conversion_days}_days"

        return False, ""

    async def get_adjustment_recommendation(
        self, group: Group, recent_events: dict
    ) -> dict:
        """
        Get recommendation for group level adjustment.

        Args:
            group: Group instance
            recent_events: Recent events for evaluation

        Returns:
            Dictionary with recommendation
        """
        should_downgrade, downgrade_reason = await self.should_auto_downgrade(
            group, recent_events
        )
        should_upgrade, upgrade_reason = await self.should_auto_upgrade(
            group, recent_events
        )

        current_score = await self.calculate_total_score(group)

        recommendation = {
            "current_level": group.level.value,
            "current_score": current_score,
            "should_adjust": False,
            "action": None,
            "reason": None,
            "target_level": None,
        }

        if should_downgrade and group.level != GroupLevel.C:
            recommendation["should_adjust"] = True
            recommendation["action"] = "downgrade"

            current_idx = list(GroupLevel).index(group.level)
            if current_idx > 0:
                recommendation["target_level"] = list(GroupLevel)[current_idx - 1].value

            recommendation["reason"] = downgrade_reason

        elif should_upgrade and group.level != GroupLevel.A:
            recommendation["should_adjust"] = True
            recommendation["action"] = "upgrade"

            current_idx = list(GroupLevel).index(group.level)
            if current_idx < len(GroupLevel) - 1:
                recommendation["target_level"] = list(GroupLevel)[current_idx + 1].value

            recommendation["reason"] = upgrade_reason

        return recommendation
