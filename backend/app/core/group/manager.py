"""
Group Manager Module

Manages Telegram group lifecycle, scoring, and operations.

Features:
- Group CRUD operations
- Group scoring and level calculation
- Group operation configuration (from database)
- Deduplication checking
"""

from datetime import datetime
from typing import Optional

import structlog
from sqlalchemy import select, delete, func, or_, cast, String
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.core.group.models import Group, GroupAccountMembership, GroupLevel, GroupLevelConfig
from app.core.group.scorer import GroupScorer
from app.exceptions import GroupNotFoundError, ValidationError

logger = structlog.get_logger()

# Default level configs (used when DB is empty)
DEFAULT_LEVEL_CONFIGS = {
    GroupLevel.A: {
        "min_score": 70.0,
        "can_send_ads": True,
        "can_mention_users": True,
        "can_share_links": True,
        "can_initiate_private": True,
        "daily_message_limit": 10,
        "message_interval": 60,
        "private_message_interval": 5,
        "rule_weight": 0.30,
        "admin_weight": 0.25,
        "history_weight": 0.20,
        "convert_weight": 0.15,
        "activity_weight": 0.10,
    },
    GroupLevel.B: {
        "min_score": 50.0,
        "can_send_ads": True,
        "can_mention_users": False,
        "can_share_links": True,
        "can_initiate_private": True,
        "daily_message_limit": 5,
        "message_interval": 180,
        "private_message_interval": 5,
        "rule_weight": 0.30,
        "admin_weight": 0.25,
        "history_weight": 0.20,
        "convert_weight": 0.15,
        "activity_weight": 0.10,
    },
    GroupLevel.C: {
        "min_score": 0.0,
        "can_send_ads": False,
        "can_mention_users": False,
        "can_share_links": False,
        "can_initiate_private": True,
        "daily_message_limit": 2,
        "message_interval": 300,
        "private_message_interval": 5,
        "rule_weight": 0.30,
        "admin_weight": 0.25,
        "history_weight": 0.20,
        "convert_weight": 0.15,
        "activity_weight": 0.10,
    },
    GroupLevel.UNRATED: {
        "min_score": -1.0,
        "can_send_ads": False,
        "can_mention_users": False,
        "can_share_links": False,
        "can_initiate_private": False,
        "daily_message_limit": 0,
        "message_interval": 600,
        "private_message_interval": 30,
        "rule_weight": 0.30,
        "admin_weight": 0.25,
        "history_weight": 0.20,
        "convert_weight": 0.15,
        "activity_weight": 0.10,
    },
}


class GroupManager:
    """
    Telegram group lifecycle manager.

    Manages group information, scoring, and operations based on group levels.
    Configurations are loaded from database with fallback to defaults.

    Attributes:
        - Provides CRUD operations for groups
        - Calculates group levels based on scoring metrics
        - Returns operation configurations based on group level
        - Handles group deduplication
    """

    def __init__(self, db: AsyncSession):
        """
        Initialize GroupManager with database session.

        Args:
            db: SQLAlchemy async session
        """
        self.db = db
        self.scorer = GroupScorer(self)
        self.logger = logger.bind(module="group_manager")
        self._config_cache: dict[GroupLevel, dict] = {}
        self._weights_cache: dict = {}

    async def _load_config_cache(self) -> None:
        """Load level configs from database into cache."""
        if self._config_cache:
            return

        result = await self.db.execute(select(GroupLevelConfig))
        configs = result.scalars().all()

        for config in configs:
            self._config_cache[config.level] = {
                "min_score": float(config.min_score),
                "can_send_ads": config.can_send_ads,
                "can_mention_users": config.can_mention_users,
                "can_share_links": config.can_share_links,
                "can_initiate_private": config.can_initiate_private,
                "daily_message_limit": config.daily_message_limit,
                "message_interval": config.message_interval,
                "private_message_interval": config.private_message_interval,
                "rule_weight": float(config.rule_weight),
                "admin_weight": float(config.admin_weight),
                "history_weight": float(config.history_weight),
                "convert_weight": float(config.convert_weight),
                "activity_weight": float(config.activity_weight),
                "auto_downgrade_kick_threshold": config.auto_downgrade_kick_threshold,
                "auto_downgrade_warning_threshold": config.auto_downgrade_warning_threshold,
                "auto_downgrade_success_rate_threshold": float(config.auto_downgrade_success_rate_threshold),
                "auto_upgrade_no_warning_days": config.auto_upgrade_no_warning_days,
                "auto_upgrade_high_success_days": config.auto_upgrade_high_success_days,
                "auto_upgrade_high_convert_days": config.auto_upgrade_high_convert_days,
            }

        for level, default_config in DEFAULT_LEVEL_CONFIGS.items():
            if level not in self._config_cache:
                self._config_cache[level] = default_config.copy()

    def _get_config(self, level: GroupLevel) -> dict:
        """Get config for a level from cache or defaults."""
        return self._config_cache.get(level, DEFAULT_LEVEL_CONFIGS.get(level, {}))

    async def ensure_default_configs(self) -> None:
        """Ensure default level configs exist in database."""
        await self._load_config_cache()

        for level, default_config in DEFAULT_LEVEL_CONFIGS.items():
            result = await self.db.execute(
                select(GroupLevelConfig).where(GroupLevelConfig.level == level)
            )
            if result.scalar_one_or_none() is None:
                config = GroupLevelConfig(
                    level=level,
                    min_score=default_config["min_score"],
                    can_send_ads=default_config["can_send_ads"],
                    can_mention_users=default_config["can_mention_users"],
                    can_share_links=default_config["can_share_links"],
                    can_initiate_private=default_config["can_initiate_private"],
                    daily_message_limit=default_config["daily_message_limit"],
                    message_interval=default_config["message_interval"],
                    private_message_interval=default_config["private_message_interval"],
                    rule_weight=default_config["rule_weight"],
                    admin_weight=default_config["admin_weight"],
                    history_weight=default_config["history_weight"],
                    convert_weight=default_config["convert_weight"],
                    activity_weight=default_config["activity_weight"],
                )
                self.db.add(config)

        await self.db.commit()
        self._config_cache.clear()

    async def get_level_config(self, level: GroupLevel) -> dict:
        """
        Get configuration for a specific level.

        Args:
            level: Group level

        Returns:
            Configuration dictionary
        """
        await self._load_config_cache()
        return self._get_config(level).copy()

    async def list_level_configs(self) -> list[GroupLevelConfig]:
        """
        List all level configurations.

        Returns:
            List of GroupLevelConfig instances
        """
        await self._load_config_cache()
        result = await self.db.execute(select(GroupLevelConfig))
        return list(result.scalars().all())

    async def update_level_config(
        self,
        level: GroupLevel,
        **kwargs,
    ) -> GroupLevelConfig:
        """
        Update configuration for a specific level.

        Args:
            level: Group level to update
            **kwargs: Configuration fields to update

        Returns:
            Updated GroupLevelConfig
        """
        await self._load_config_cache()

        result = await self.db.execute(
            select(GroupLevelConfig).where(GroupLevelConfig.level == level)
        )
        config = result.scalar_one_or_none()

        if config is None:
            raise ValidationError(f"Level config for {level.value} not found")

        allowed_fields = [
            "min_score", "can_send_ads", "can_mention_users", "can_share_links",
            "can_initiate_private", "daily_message_limit", "message_interval",
            "private_message_interval", "rule_weight", "admin_weight",
            "history_weight", "convert_weight", "activity_weight",
            "auto_downgrade_kick_threshold", "auto_downgrade_warning_threshold",
            "auto_downgrade_success_rate_threshold", "auto_upgrade_no_warning_days",
            "auto_upgrade_high_success_days", "auto_upgrade_high_convert_days",
            "description",
        ]

        for field, value in kwargs.items():
            if field in allowed_fields and value is not None:
                setattr(config, field, value)

        await self.db.commit()
        await self.db.refresh(config)

        self._config_cache.clear()
        self.logger.info(
            "level_config_updated",
            level=level.value,
            updated_fields=list(kwargs.keys()),
        )

        return config

    async def create_group(
        self,
        group_id: int,
        title: Optional[str] = None,
        username: Optional[str] = None,
        member_count: int = 0,
        status: str = "active",
        discovery_source: str = "manual",
        source_keyword: Optional[str] = None,
    ) -> Group:
        """
        Create a new group entry.

        Args:
            group_id: Telegram group ID
            title: Group title
            username: Group username
            member_count: Number of members

        Returns:
            Created Group instance
        """
        existing = await self.get_group_by_telegram_id(group_id)
        if existing:
            raise ValidationError(f"Group {group_id} already exists")

        group = Group(
            group_id=group_id,
            title=title,
            username=username,
            member_count=member_count,
            status=status,
            discovery_source=discovery_source,
            source_keyword=source_keyword,
            level=GroupLevel.UNRATED,
        )

        self.db.add(group)
        await self.db.commit()
        await self.db.refresh(group)

        self.logger.info(
            "group_created",
            group_id=group.id,
            telegram_id=group_id,
            title=title,
        )

        return group

    async def get_group(self, group_id: int) -> Optional[Group]:
        """
        Get group by database ID.

        Args:
            group_id: Group database ID

        Returns:
            Group if found, None otherwise
        """
        result = await self.db.execute(
            select(Group).where(Group.id == group_id)
        )
        return result.scalar_one_or_none()

    async def get_group_by_telegram_id(self, telegram_id: int) -> Optional[Group]:
        """
        Get group by Telegram group ID.

        Args:
            telegram_id: Telegram group ID

        Returns:
            Group if found, None otherwise
        """
        result = await self.db.execute(
            select(Group).where(Group.group_id == telegram_id)
        )
        return result.scalar_one_or_none()

    async def get_group_by_id(self, group_id: int) -> Optional[Group]:
        """Backward-compatible alias for fetching by Telegram group ID."""
        return await self.get_group_by_telegram_id(group_id)

    async def list_groups(
        self,
        level: Optional[GroupLevel] = None,
        status: Optional[str] = None,
        keyword: Optional[str] = None,
        source_keyword: Optional[str] = None,
        min_members: Optional[int] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Group]:
        """
        List groups with optional level filter.

        Args:
            level: Optional level filter
            limit: Maximum results
            offset: Pagination offset

        Returns:
            List of groups
        """
        await self._load_config_cache()

        query = select(Group)

        if level:
            query = query.where(Group.level == level)
        if status:
            query = query.where(Group.status == status)
        if source_keyword:
            query = query.where(Group.source_keyword == source_keyword)
        if min_members is not None:
            query = query.where(Group.member_count >= min_members)
        if keyword:
            keyword_pattern = f"%{keyword}%"
            query = query.where(
                or_(
                    Group.title.ilike(keyword_pattern),
                    Group.username.ilike(keyword_pattern),
                    Group.source_keyword.ilike(keyword_pattern),
                    cast(Group.group_id, String).like(keyword_pattern),
                )
            )

        query = query.limit(limit).offset(offset).order_by(Group.level_score.desc())

        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def count_groups(
        self,
        level: Optional[GroupLevel] = None,
        status: Optional[str] = None,
        keyword: Optional[str] = None,
        source_keyword: Optional[str] = None,
        min_members: Optional[int] = None,
    ) -> int:
        """Count groups with optional filters."""
        query = select(func.count(Group.id))

        if level:
            query = query.where(Group.level == level)
        if status:
            query = query.where(Group.status == status)
        if source_keyword:
            query = query.where(Group.source_keyword == source_keyword)
        if min_members is not None:
            query = query.where(Group.member_count >= min_members)
        if keyword:
            keyword_pattern = f"%{keyword}%"
            query = query.where(
                or_(
                    Group.title.ilike(keyword_pattern),
                    Group.username.ilike(keyword_pattern),
                    Group.source_keyword.ilike(keyword_pattern),
                    cast(Group.group_id, String).like(keyword_pattern),
                )
            )

        result = await self.db.execute(query)
        return result.scalar() or 0

    async def get_groups_by_level(self, level: GroupLevel) -> list[Group]:
        """
        Get all groups of a specific level.

        Args:
            level: Group level to filter

        Returns:
            List of groups with specified level
        """
        result = await self.db.execute(
            select(Group)
            .where(Group.level == level)
            .order_by(Group.level_score.desc())
        )
        return list(result.scalars().all())

    async def update_group(
        self,
        group_id: int,
        title: Optional[str] = None,
        username: Optional[str] = None,
        member_count: Optional[int] = None,
        status: Optional[str] = None,
        discovery_source: Optional[str] = None,
        source_keyword: Optional[str] = None,
    ) -> Group:
        """
        Update group information.

        Args:
            group_id: Group database ID
            title: New title
            username: New username
            member_count: New member count

        Returns:
            Updated Group

        Raises:
            GroupNotFoundError: If group not found
        """
        group = await self.get_group(group_id)
        if not group:
            raise GroupNotFoundError(f"Group {group_id} not found")

        if title is not None:
            group.title = title
        if username is not None:
            group.username = username
        if member_count is not None:
            group.member_count = member_count
        if status is not None:
            group.status = status
        if discovery_source is not None:
            group.discovery_source = discovery_source
        if source_keyword is not None:
            group.source_keyword = source_keyword

        await self.db.commit()
        await self.db.refresh(group)

        self.logger.info("group_updated", group_id=group_id)

        return group

    async def record_account_membership(
        self,
        group_id: int,
        account_id: int,
        status: str = "joined",
        join_method: str = "manual",
        source_keyword: Optional[str] = None,
        note: Optional[str] = None,
    ) -> GroupAccountMembership:
        """
        Record that a Telegram account joined a managed group.

        Args:
            group_id: Group database ID
            account_id: Telegram account database ID
            status: Membership status
            join_method: How the account joined the group
            source_keyword: Keyword that led to this group
            note: Optional operator note
        """
        group = await self.get_group(group_id)
        if not group:
            raise GroupNotFoundError(f"Group {group_id} not found")

        existing_result = await self.db.execute(
            select(GroupAccountMembership).where(
                GroupAccountMembership.group_id == group_id,
                GroupAccountMembership.account_id == account_id,
            )
        )
        existing = existing_result.scalar_one_or_none()
        if existing:
            raise ValidationError("This account has already joined this group")

        membership = GroupAccountMembership(
            group_id=group.id,
            telegram_group_id=group.group_id,
            account_id=account_id,
            status=status,
            join_method=join_method,
            source_keyword=source_keyword or group.source_keyword,
            note=note,
        )
        self.db.add(membership)

        try:
            await self.db.commit()
            await self.db.refresh(membership)
        except IntegrityError as exc:
            await self.db.rollback()
            raise ValidationError("This account has already joined this group") from exc

        self.logger.info(
            "group_account_membership_recorded",
            group_id=group_id,
            telegram_group_id=group.group_id,
            account_id=account_id,
        )

        return membership

    async def list_account_memberships(self, group_id: int) -> list[GroupAccountMembership]:
        """List account memberships for a group database ID."""
        result = await self.db.execute(
            select(GroupAccountMembership)
            .where(GroupAccountMembership.group_id == group_id)
            .order_by(GroupAccountMembership.created_at.desc())
        )
        return list(result.scalars().all())

    async def add_group(
        self,
        group_id: int,
        title: Optional[str] = None,
        username: Optional[str] = None,
        member_count: int = 0,
        source_keyword: Optional[str] = None,
        discovery_source: str = "keyword_search",
    ) -> Group:
        """Backward-compatible helper for acquisition search modules."""
        return await self.create_group(
            group_id=group_id,
            title=title,
            username=username,
            member_count=member_count,
            source_keyword=source_keyword,
            discovery_source=discovery_source,
        )

    async def update_scores(
        self,
        group_id: int,
        rule_score: Optional[int] = None,
        admin_score: Optional[int] = None,
        history_score: Optional[int] = None,
        convert_score: Optional[int] = None,
        activity_score: Optional[int] = None,
    ) -> Group:
        """
        Update group scoring metrics.

        Args:
            group_id: Group database ID
            rule_score: Group rules control score (0-100)
            admin_score: Admin attitude score (0-100)
            history_score: Bot history score (0-100)
            convert_score: Conversion effect score (0-100)
            activity_score: Group activity score (0-100)

        Returns:
            Updated Group with recalculated level
        """
        group = await self.get_group(group_id)
        if not group:
            raise GroupNotFoundError(f"Group {group_id} not found")

        if rule_score is not None:
            group.rule_score = max(0, min(100, rule_score))
        if admin_score is not None:
            group.admin_score = max(0, min(100, admin_score))
        if history_score is not None:
            group.history_score = max(0, min(100, history_score))
        if convert_score is not None:
            group.convert_score = max(0, min(100, convert_score))
        if activity_score is not None:
            group.activity_score = max(0, min(100, activity_score))

        total_score = await self.scorer.calculate_total_score(group)
        group.level_score = total_score
        new_level = await self.scorer.calculate_level(group)
        old_level = group.level

        group.level = new_level

        await self.db.commit()
        await self.db.refresh(group)

        if old_level != new_level:
            self.logger.info(
                "group_level_changed",
                group_id=group_id,
                old_level=old_level.value,
                new_level=new_level.value,
                level_score=group.level_score,
            )

        return group

    async def adjust_level(
        self,
        group_id: int,
        reason: str,
        new_level: Optional[GroupLevel] = None,
    ) -> Group:
        """
        Manually adjust group level.

        Args:
            group_id: Group database ID
            reason: Reason for adjustment
            new_level: Optional new level (if None, recalculates)

        Returns:
            Adjusted Group

        Raises:
            GroupNotFoundError: If group not found
        """
        group = await self.get_group(group_id)
        if not group:
            raise GroupNotFoundError(f"Group {group_id} not found")

        old_level = group.level

        if new_level is not None:
            group.level = new_level
            self.logger.info(
                "group_level_manual_adjust",
                group_id=group_id,
                reason=reason,
                old_level=old_level.value,
                new_level=new_level.value,
            )
        else:
            group.level = await self.scorer.calculate_level(group)
            self.logger.info(
                "group_level_recalculated",
                group_id=group_id,
                old_level=old_level.value,
                new_level=group.level.value,
            )

        await self.db.commit()
        await self.db.refresh(group)

        return group

    async def delete_group(self, group_id: int) -> bool:
        """
        Delete a group.

        Args:
            group_id: Group database ID

        Returns:
            True if deleted

        Raises:
            GroupNotFoundError: If group not found
        """
        group = await self.get_group(group_id)
        if not group:
            raise GroupNotFoundError(f"Group {group_id} not found")

        await self.db.execute(delete(Group).where(Group.id == group_id))
        await self.db.commit()

        self.logger.info("group_deleted", group_id=group_id, telegram_id=group.group_id)

        return True

    async def get_operation_config(self, group: Group) -> dict:
        """
        Get operation configuration for a group based on its level.

        Args:
            group: Group instance

        Returns:
            Operation configuration dictionary
        """
        await self._load_config_cache()
        config = self._get_config(group.level)

        return {
            "can_send_ads": config.get("can_send_ads", False),
            "can_mention_users": config.get("can_mention_users", False),
            "can_share_links": config.get("can_share_links", False),
            "can_initiate_private": config.get("can_initiate_private", False),
            "daily_message_limit": config.get("daily_message_limit", 0),
            "message_interval": config.get("message_interval", 60),
            "private_message_interval": config.get("private_message_interval", 30),
        }

    async def deduplicate(self, group_id: int) -> bool:
        """
        Check if group is a duplicate.

        Args:
            group_id: Telegram group ID to check

        Returns:
            True if duplicate exists, False otherwise
        """
        existing = await self.get_group_by_telegram_id(group_id)
        return existing is not None

    async def deduplicate_by_username(self, username: str) -> bool:
        """
        Check if group username already exists.

        Args:
            username: Group username to check

        Returns:
            True if duplicate exists, False otherwise
        """
        result = await self.db.execute(
            select(Group).where(Group.username == username)
        )
        return result.scalar_one_or_none() is not None

    async def get_group_stats(self) -> dict:
        """
        Get statistics about all groups.

        Returns:
            Dictionary with group statistics
        """
        result = await self.db.execute(
            select(
                func.count(Group.id).label("total"),
                func.avg(Group.level_score).label("avg_score"),
            )
        )
        row = result.one()

        level_counts = {}
        for level in GroupLevel:
            count_result = await self.db.execute(
                select(func.count(Group.id)).where(Group.level == level)
            )
            level_counts[level.value] = count_result.scalar()

        return {
            "total_groups": row.total or 0,
            "average_score": float(row.avg_score or 0),
            "level_distribution": level_counts,
        }
