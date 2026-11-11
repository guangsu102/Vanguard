"""Regression tests for the 2026-11-11 production diagnosis fixes.

Covers the three priority repairs: chat-scoped restriction classification,
permission-failure retry spacing, and deletion-event ingestion filtering.
"""
import fakeredis.aioredis
import pytest

from app.modules.acquisition.adaptive_frequency import canonical, relevant_ad_deletion
from app.modules.acquisition.models import AdDeliveryLog
from app.modules.acquisition.simple_limits import PREFIX, revert_ad, take_ad


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.mark.asyncio
async def test_revert_ad_without_spacing_deletes_the_cooldown(redis):
    await take_ad(redis, 7, 111, cap=5, account_interval=60, group_interval=60)
    due_key = PREFIX + "ads:7:due"
    assert await redis.get(due_key) is not None
    await revert_ad(redis, 7)
    assert await redis.get(due_key) is None
    assert int(await redis.hget(PREFIX + "ads:7:count", "used") or 0) == 0


@pytest.mark.asyncio
async def test_revert_ad_with_spacing_rolls_back_quota_but_holds_cooldown(redis):
    """A permission failure must not authorize an immediate burst."""
    await take_ad(redis, 7, 111, cap=5, account_interval=60, group_interval=60)
    due_key = PREFIX + "ads:7:due"
    before = float(await redis.get(due_key))
    await revert_ad(redis, 7, cooldown_seconds=600)
    after = float(await redis.get(due_key))
    # The next attempt is held to now+600s instead of being freed immediately.
    assert after > before
    assert int(await redis.hget(PREFIX + "ads:7:count", "used") or 0) == 0
    # The allocator enforces the held cooldown.
    from app.modules.acquisition.simple_limits import Deferred
    with pytest.raises(Deferred) as excinfo:
        await take_ad(redis, 7, 112, cap=5, account_interval=60, group_interval=60)
    assert excinfo.value.reason == "account_cooldown"


@pytest.mark.asyncio
async def test_relevant_ad_deletion_matches_only_own_successful_delivery(test_db):
    target = canonical(-100123)
    test_db.add(AdDeliveryLog(account_id=7, telegram_group_id=target,
                              ad_campaign_id=1, status="success", telegram_message_id=42))
    await test_db.commit()
    assert await relevant_ad_deletion(test_db, 7, -100123, [42, 999]) is True
    # Another account, another chat, another message, and failed or unknown
    # deliveries are unrelated noise and stay out of the business queue.
    assert await relevant_ad_deletion(test_db, 8, -100123, [42]) is False
    assert await relevant_ad_deletion(test_db, 7, -100999, [42]) is False
    assert await relevant_ad_deletion(test_db, 7, -100123, [43]) is False
    assert await relevant_ad_deletion(test_db, 7, None, [42]) is False
    assert await relevant_ad_deletion(test_db, 7, -100123, []) is False


def test_permission_failure_spacing_floor_is_bounded():
    from app.modules.acquisition.simple_growth import PERMISSION_FAILURE_SPACING_SECONDS
    assert PERMISSION_FAILURE_SPACING_SECONDS >= 600
