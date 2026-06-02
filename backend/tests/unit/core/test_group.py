"""
Unit Tests for Group Management Module

Tests cover:
- Group CRUD operations
- Level calculation
- Score updates
- Configuration management
- Auto-adjustment logic
"""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.core.database import Base
from app.core.group import GroupManager, GroupScorer, Group, GroupLevel, GroupLevelConfig


TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture(scope="function")
async def db_session():
    """Create test database session."""
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    await engine.dispose()


@pytest_asyncio.fixture
async def manager(db_session: AsyncSession):
    """Create GroupManager with test database."""
    mgr = GroupManager(db_session)
    await mgr.ensure_default_configs()
    return mgr


@pytest_asyncio.fixture
async def scorer(manager: GroupManager):
    """Create GroupScorer with manager."""
    return GroupScorer(manager)


@pytest_asyncio.fixture
async def sample_group(manager: GroupManager) -> Group:
    """Create a sample group for testing."""
    return await manager.create_group(
        group_id=123456789,
        title="Test Group",
        username="testgroup",
        member_count=100,
    )


# ============================================================================
# Test Group CRUD Operations
# ============================================================================

class TestGroupCRUD:
    """Test group CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_group(self, manager: GroupManager):
        """Test group creation."""
        group = await manager.create_group(
            group_id=111111,
            title="New Group",
            username="newgroup",
            member_count=50,
        )

        assert group.id is not None
        assert group.group_id == 111111
        assert group.title == "New Group"
        assert group.username == "newgroup"
        assert group.member_count == 50
        assert group.level == GroupLevel.UNRATED

    @pytest.mark.asyncio
    async def test_create_duplicate_group(self, manager: GroupManager, sample_group: Group):
        """Test duplicate group creation raises error."""
        from app.core.exceptions import ValidationError

        with pytest.raises(ValidationError, match="already exists"):
            await manager.create_group(group_id=123456789)

    @pytest.mark.asyncio
    async def test_get_group(self, manager: GroupManager, sample_group: Group):
        """Test getting group by ID."""
        group = await manager.get_group(sample_group.id)

        assert group is not None
        assert group.id == sample_group.id
        assert group.group_id == 123456789

    @pytest.mark.asyncio
    async def test_get_group_by_telegram_id(self, manager: GroupManager, sample_group: Group):
        """Test getting group by Telegram ID."""
        group = await manager.get_group_by_telegram_id(123456789)

        assert group is not None
        assert group.group_id == 123456789

    @pytest.mark.asyncio
    async def test_get_nonexistent_group(self, manager: GroupManager):
        """Test getting nonexistent group returns None."""
        group = await manager.get_group(99999)
        assert group is None

    @pytest.mark.asyncio
    async def test_update_group(self, manager: GroupManager, sample_group: Group):
        """Test updating group information."""
        updated = await manager.update_group(
            group_id=sample_group.id,
            title="Updated Title",
            member_count=200,
        )

        assert updated.title == "Updated Title"
        assert updated.member_count == 200
        assert updated.username == "testgroup"

    @pytest.mark.asyncio
    async def test_update_nonexistent_group(self, manager: GroupManager):
        """Test updating nonexistent group raises error."""
        from app.core.exceptions import GroupNotFoundError

        with pytest.raises(GroupNotFoundError):
            await manager.update_group(group_id=99999, title="Test")

    @pytest.mark.asyncio
    async def test_delete_group(self, manager: GroupManager, sample_group: Group):
        """Test deleting a group."""
        result = await manager.delete_group(sample_group.id)

        assert result is True
        assert await manager.get_group(sample_group.id) is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_group(self, manager: GroupManager):
        """Test deleting nonexistent group raises error."""
        from app.core.exceptions import GroupNotFoundError

        with pytest.raises(GroupNotFoundError):
            await manager.delete_group(99999)

    @pytest.mark.asyncio
    async def test_list_groups(self, manager: GroupManager):
        """Test listing groups."""
        await manager.create_group(group_id=1001, title="Group 1")
        await manager.create_group(group_id=1002, title="Group 2")
        await manager.create_group(group_id=1003, title="Group 3")

        groups = await manager.list_groups(limit=10)

        assert len(groups) >= 3

    @pytest.mark.asyncio
    async def test_list_groups_by_level(self, manager: GroupManager):
        """Test listing groups filtered by level."""
        g1 = await manager.create_group(group_id=2001)
        g2 = await manager.create_group(group_id=2002)

        await manager.adjust_level(g1.id, reason="test", new_level=GroupLevel.A)
        await manager.adjust_level(g2.id, reason="test", new_level=GroupLevel.B)

        a_groups = await manager.get_groups_by_level(GroupLevel.A)
        b_groups = await manager.get_groups_by_level(GroupLevel.B)

        assert any(g.group_id == 2001 for g in a_groups)
        assert any(g.group_id == 2002 for g in b_groups)


# ============================================================================
# Test Score Updates
# ============================================================================

class TestScoreUpdates:
    """Test score update operations."""

    @pytest.mark.asyncio
    async def test_update_scores(self, manager: GroupManager, sample_group: Group):
        """Test updating group scores."""
        updated = await manager.update_scores(
            group_id=sample_group.id,
            rule_score=80,
            admin_score=70,
            history_score=60,
            convert_score=50,
            activity_score=40,
        )

        assert updated.rule_score == 80
        assert updated.admin_score == 70
        assert updated.history_score == 60
        assert updated.convert_score == 50
        assert updated.activity_score == 40

    @pytest.mark.asyncio
    async def test_update_scores_clamping(self, manager: GroupManager, sample_group: Group):
        """Test that scores are clamped to 0-100 range."""
        updated = await manager.update_scores(
            group_id=sample_group.id,
            rule_score=150,
            admin_score=-20,
        )

        assert updated.rule_score == 100
        assert updated.admin_score == 0

    @pytest.mark.asyncio
    async def test_partial_score_update(self, manager: GroupManager, sample_group: Group):
        """Test partial score update."""
        await manager.update_scores(
            group_id=sample_group.id,
            rule_score=80,
        )

        updated = await manager.update_scores(
            group_id=sample_group.id,
            admin_score=60,
        )

        assert updated.rule_score == 80
        assert updated.admin_score == 60


# ============================================================================
# Test Level Calculation
# ============================================================================

class TestLevelCalculation:
    """Test level calculation and adjustment."""

    @pytest.mark.asyncio
    async def test_level_calculation_a(self, manager: GroupManager, scorer: GroupScorer, sample_group: Group):
        """Test level A calculation (score >= 70)."""
        await manager.update_scores(
            group_id=sample_group.id,
            rule_score=100,
            admin_score=100,
            history_score=100,
            convert_score=100,
            activity_score=100,
        )

        group = await manager.get_group(sample_group.id)
        level = await scorer.calculate_level(group)

        assert level == GroupLevel.A

    @pytest.mark.asyncio
    async def test_level_calculation_b(self, manager: GroupManager, scorer: GroupScorer, sample_group: Group):
        """Test level B calculation (score >= 50)."""
        await manager.update_scores(
            group_id=sample_group.id,
            rule_score=70,
            admin_score=70,
            history_score=50,
            convert_score=30,
            activity_score=10,
        )

        group = await manager.get_group(sample_group.id)
        level = await scorer.calculate_level(group)

        assert level == GroupLevel.B

    @pytest.mark.asyncio
    async def test_level_calculation_c(self, manager: GroupManager, scorer: GroupScorer, sample_group: Group):
        """Test level C calculation (score < 50)."""
        await manager.update_scores(
            group_id=sample_group.id,
            rule_score=30,
            admin_score=30,
            history_score=30,
            convert_score=30,
            activity_score=30,
        )

        group = await manager.get_group(sample_group.id)
        level = await scorer.calculate_level(group)

        assert level == GroupLevel.C

    @pytest.mark.asyncio
    async def test_manual_level_adjustment(self, manager: GroupManager, sample_group: Group):
        """Test manual level adjustment."""
        await manager.update_scores(
            group_id=sample_group.id,
            rule_score=10,
            admin_score=10,
            history_score=10,
            convert_score=10,
            activity_score=10,
        )

        adjusted = await manager.adjust_level(
            group_id=sample_group.id,
            reason="manual override",
            new_level=GroupLevel.A,
        )

        assert adjusted.level == GroupLevel.A

    @pytest.mark.asyncio
    async def test_level_recalculation(self, manager: GroupManager, sample_group: Group):
        """Test level recalculation when new_level is None."""
        await manager.adjust_level(
            group_id=sample_group.id,
            reason="initial",
            new_level=GroupLevel.UNRATED,
        )

        await manager.update_scores(
            group_id=sample_group.id,
            rule_score=90,
            admin_score=90,
            history_score=90,
            convert_score=90,
            activity_score=90,
        )

        adjusted = await manager.adjust_level(
            group_id=sample_group.id,
            reason="recalculate",
            new_level=None,
        )

        assert adjusted.level == GroupLevel.A


# ============================================================================
# Test Configuration Management
# ============================================================================

class TestConfiguration:
    """Test level configuration management."""

    @pytest.mark.asyncio
    async def test_ensure_default_configs(self, manager: GroupManager, db_session: AsyncSession):
        """Test default configs are created."""
        from sqlalchemy import select

        result = await db_session.execute(select(GroupLevelConfig))
        configs = result.scalars().all()

        assert len(configs) == 4
        levels = {c.level for c in configs}
        assert levels == {GroupLevel.A, GroupLevel.B, GroupLevel.C, GroupLevel.UNRATED}

    @pytest.mark.asyncio
    async def test_get_level_config(self, manager: GroupManager):
        """Test getting level config."""
        config = await manager.get_level_config(GroupLevel.A)

        assert config["min_score"] == 70.0
        assert config["can_send_ads"] is True
        assert config["daily_message_limit"] == 10

    @pytest.mark.asyncio
    async def test_update_level_config(self, manager: GroupManager):
        """Test updating level config."""
        updated = await manager.update_level_config(
            level=GroupLevel.A,
            min_score=75.0,
            daily_message_limit=15,
            description="High value group",
        )

        assert float(updated.min_score) == 75.0
        assert updated.daily_message_limit == 15
        assert updated.description == "High value group"

    @pytest.mark.asyncio
    async def test_list_level_configs(self, manager: GroupManager):
        """Test listing all level configs."""
        configs = await manager.list_level_configs()

        assert len(configs) == 4
        assert all(isinstance(c, GroupLevelConfig) for c in configs)

    @pytest.mark.asyncio
    async def test_operation_config_for_group(self, manager: GroupManager, sample_group: Group):
        """Test getting operation config for a group."""
        await manager.adjust_level(sample_group.id, reason="test", new_level=GroupLevel.A)

        config = await manager.get_operation_config(sample_group)

        assert config["can_send_ads"] is True
        assert config["can_mention_users"] is True
        assert config["daily_message_limit"] == 10


# ============================================================================
# Test Deduplication
# ============================================================================

class TestDeduplication:
    """Test group deduplication methods."""

    @pytest.mark.asyncio
    async def test_deduplicate_existing(self, manager: GroupManager, sample_group: Group):
        """Test deduplication returns True for existing group."""
        result = await manager.deduplicate(123456789)
        assert result is True

    @pytest.mark.asyncio
    async def test_deduplicate_nonexisting(self, manager: GroupManager):
        """Test deduplication returns False for new group."""
        result = await manager.deduplicate(999999999)
        assert result is False

    @pytest.mark.asyncio
    async def test_deduplicate_by_username(self, manager: GroupManager, sample_group: Group):
        """Test deduplication by username."""
        result = await manager.deduplicate_by_username("testgroup")
        assert result is True

        result = await manager.deduplicate_by_username("nonexistent")
        assert result is False


# ============================================================================
# Test Statistics
# ============================================================================

class TestStatistics:
    """Test statistics methods."""

    @pytest.mark.asyncio
    async def test_get_group_stats(self, manager: GroupManager):
        """Test getting group statistics."""
        await manager.create_group(group_id=3001)
        await manager.create_group(group_id=3002)

        stats = await manager.get_group_stats()

        assert stats["total_groups"] >= 2
        assert "average_score" in stats
        assert "level_distribution" in stats
        assert isinstance(stats["level_distribution"], dict)


# ============================================================================
# Test Score Breakdown
# ============================================================================

class TestScoreBreakdown:
    """Test score breakdown functionality."""

    @pytest.mark.asyncio
    async def test_get_score_breakdown(self, manager: GroupManager, scorer: GroupScorer, sample_group: Group):
        """Test getting score breakdown."""
        await manager.update_scores(
            group_id=sample_group.id,
            rule_score=80,
            admin_score=70,
            history_score=60,
            convert_score=50,
            activity_score=40,
        )

        breakdown = await scorer.get_score_breakdown(sample_group)

        assert "total_score" in breakdown
        assert "dimensions" in breakdown
        assert "level" in breakdown
        assert breakdown["dimensions"]["rule_score"]["value"] == 80
        assert breakdown["dimensions"]["rule_score"]["weight"] == 0.30


# ============================================================================
# Test Auto Adjustment
# ============================================================================

class TestAutoAdjustment:
    """Test auto-adjustment logic."""

    @pytest.mark.asyncio
    async def test_should_auto_downgrade_kick(self, scorer: GroupScorer, sample_group: Group):
        """Test auto-downgrade on kick."""
        events = {"kicked_count": 3, "warning_count": 0, "success_rate": 0.9}

        should_downgrade, reason = await scorer.should_auto_downgrade(sample_group, events)

        assert should_downgrade is True
        assert "kicked" in reason

    @pytest.mark.asyncio
    async def test_should_auto_downgrade_low_success(self, scorer: GroupScorer, sample_group: Group):
        """Test auto-downgrade on low success rate."""
        events = {"kicked_count": 0, "warning_count": 0, "success_rate": 0.3}

        should_downgrade, reason = await scorer.should_auto_downgrade(sample_group, events)

        assert should_downgrade is True
        assert "low_success" in reason

    @pytest.mark.asyncio
    async def test_should_not_auto_downgrade(self, scorer: GroupScorer, sample_group: Group):
        """Test no auto-downgrade when conditions not met."""
        events = {"kicked_count": 0, "warning_count": 1, "success_rate": 0.9}

        should_downgrade, reason = await scorer.should_auto_downgrade(sample_group, events)

        assert should_downgrade is False
        assert reason == ""

    @pytest.mark.asyncio
    async def test_should_auto_upgrade(self, scorer: GroupScorer, sample_group: Group):
        """Test auto-upgrade conditions."""
        events = {
            "no_warnings_days": 35,
            "high_success_rate_days": 0,
            "high_conversion_days": 0,
        }

        should_upgrade, reason = await scorer.should_auto_upgrade(sample_group, events)

        assert should_upgrade is True
        assert "no_warnings" in reason

    @pytest.mark.asyncio
    async def test_get_adjustment_recommendation_downgrade(self, scorer: GroupScorer, sample_group: Group):
        """Test getting adjustment recommendation for downgrade."""
        events = {"kicked_count": 3, "warning_count": 0, "success_rate": 0.9}

        recommendation = await scorer.get_adjustment_recommendation(sample_group, events)

        assert recommendation["should_adjust"] is True
        assert recommendation["action"] == "downgrade"
        assert recommendation["target_level"] in ["B", "C"]

    @pytest.mark.asyncio
    async def test_get_adjustment_recommendation_no_action(self, scorer: GroupScorer, sample_group: Group):
        """Test no adjustment recommendation when not needed."""
        events = {"kicked_count": 0, "warning_count": 1, "success_rate": 0.9}

        recommendation = await scorer.get_adjustment_recommendation(sample_group, events)

        assert recommendation["should_adjust"] is False
        assert recommendation["action"] is None
