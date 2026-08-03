"""
Initial automatic rating for groups that are already joined by an account.

The score is intentionally conservative because dialog sync only gives us
basic metadata. Runtime audits and conversion metrics can later raise or lower
the same score dimensions with stronger evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.group.models import Group, GroupLevel

if TYPE_CHECKING:
    from app.core.group.scorer import GroupScorer


@dataclass(frozen=True)
class InitialGroupRating:
    """Score dimensions used to seed an unrated joined group."""

    rule_score: int
    admin_score: int
    history_score: int
    convert_score: int
    activity_score: int


def calculate_joined_group_initial_rating(
    *,
    member_count: int = 0,
    username: str | None = None,
) -> InitialGroupRating:
    """
    Build a conservative first-pass score from dialog metadata.

    The account is already in the group, so history starts neutral-positive.
    Public usernames and healthy member counts are useful quality signals, but
    conversion stays low until real campaign data exists.
    """

    members = max(0, int(member_count or 0))
    has_username = bool((username or "").strip())

    if members <= 0:
        return InitialGroupRating(
            rule_score=45 if has_username else 40,
            admin_score=25,
            history_score=20,
            convert_score=0,
            activity_score=0,
        )

    if members < 50:
        activity_score = 35
        size_bonus = 0
    elif members < 200:
        activity_score = 50
        size_bonus = 8
    elif members < 1_000:
        activity_score = 65
        size_bonus = 14
    elif members < 10_000:
        activity_score = 82
        size_bonus = 18
    elif members < 50_000:
        activity_score = 88
        size_bonus = 18
    elif members < 200_000:
        activity_score = 75
        size_bonus = 10
    else:
        activity_score = 60
        size_bonus = 4

    rule_score = 82 if has_username else 76
    admin_score = min(100, 45 + size_bonus + (12 if has_username else 0))
    history_score = 55
    convert_score = 10 if has_username and members >= 200 else 0

    return InitialGroupRating(
        rule_score=rule_score,
        admin_score=admin_score,
        history_score=history_score,
        convert_score=convert_score,
        activity_score=activity_score,
    )


def _legacy_joined_group_initial_rating(
    *,
    member_count: int = 0,
    username: str | None = None,
) -> InitialGroupRating:
    """Previous auto-rating formula, used only to identify rows to recalculate."""

    members = max(0, int(member_count or 0))
    has_username = bool((username or "").strip())

    if members < 50:
        activity_score = 35
        size_bonus = 0
    elif members < 200:
        activity_score = 50
        size_bonus = 8
    elif members < 1_000:
        activity_score = 65
        size_bonus = 14
    elif members < 10_000:
        activity_score = 82
        size_bonus = 18
    elif members < 50_000:
        activity_score = 88
        size_bonus = 18
    elif members < 200_000:
        activity_score = 75
        size_bonus = 10
    else:
        activity_score = 60
        size_bonus = 4

    return InitialGroupRating(
        rule_score=82 if has_username else 76,
        admin_score=min(100, 45 + size_bonus + (12 if has_username else 0)),
        history_score=55,
        convert_score=10 if has_username and members >= 200 else 0,
        activity_score=activity_score,
    )


def should_auto_rate_joined_group(group: Group) -> bool:
    """Return True when the group has not been manually or metrically rated."""

    level = getattr(group.level, "value", group.level)
    if level not in {None, GroupLevel.UNRATED.value, GroupLevel.UNRATED}:
        return False
    return True


def _score_value(group: Group, field: str) -> int:
    value = getattr(group, field, 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _has_existing_score_dimensions(group: Group) -> bool:
    return any(
        _score_value(group, field) > 0
        for field in (
            "rule_score",
            "admin_score",
            "history_score",
            "convert_score",
            "activity_score",
        )
    )


def _score_dimensions(group: Group) -> tuple[int, int, int, int, int]:
    return (
        _score_value(group, "rule_score"),
        _score_value(group, "admin_score"),
        _score_value(group, "history_score"),
        _score_value(group, "convert_score"),
        _score_value(group, "activity_score"),
    )


def _rating_dimensions(rating: InitialGroupRating) -> tuple[int, int, int, int, int]:
    return (
        rating.rule_score,
        rating.admin_score,
        rating.history_score,
        rating.convert_score,
        rating.activity_score,
    )


def has_auto_generated_joined_group_rating(group: Group) -> bool:
    """Detect ratings produced by current or legacy joined-group auto rating."""

    dimensions = _score_dimensions(group)
    if dimensions == (0, 0, 0, 0, 0):
        return False

    current = calculate_joined_group_initial_rating(
        member_count=group.member_count,
        username=group.username,
    )
    legacy = _legacy_joined_group_initial_rating(
        member_count=group.member_count,
        username=group.username,
    )
    return dimensions in {
        _rating_dimensions(current),
        _rating_dimensions(legacy),
    }


async def apply_joined_group_auto_rating(
    group: Group,
    scorer: "GroupScorer",
    *,
    recompute_existing_auto_rating: bool = False,
) -> bool:
    """
    Seed or recalculate rating for an unrated joined group.

    Existing non-zero score dimensions are respected. This allows older rows
    that are still marked unrated to be recalculated without losing evidence.
    """

    should_recompute = (
        recompute_existing_auto_rating
        and has_auto_generated_joined_group_rating(group)
    )

    if not should_auto_rate_joined_group(group) and not should_recompute:
        return False

    if should_recompute or not _has_existing_score_dimensions(group):
        rating = calculate_joined_group_initial_rating(
            member_count=group.member_count,
            username=group.username,
        )
        group.rule_score = rating.rule_score
        group.admin_score = rating.admin_score
        group.history_score = rating.history_score
        group.convert_score = rating.convert_score
        group.activity_score = rating.activity_score

    total_score = await scorer.calculate_total_score(group)
    group.level_score = round(total_score, 2)
    group.level = await scorer.calculate_level(group)
    return True
