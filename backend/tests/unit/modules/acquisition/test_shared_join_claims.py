"""Cross-account shared-pool ownership and release, without Telegram requests."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.group.models import Group, GroupAccountMembership
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.join_budget import JoinBudgetBlocked
from app.modules.acquisition.join_claims import occupied_group_ids
from app.modules.acquisition.models import AutoJoinAttempt, DeliveryStatus
from tests.unit.modules.acquisition.test_join_budget import _eligible_account, _service

# Keep the reservation fixture inside the live ten-minute reservation window.
# A fixed historical date makes a freshly-created reservation look expired to
# the candidate selector, which correctly evaluates occupancy against wall
# clock time.
NOW = datetime.utcnow()
pytestmark = pytest.mark.asyncio


async def shared_fixture(db):
    a, _ = await _eligible_account(db, "claim-a", now=NOW)
    b, _ = await _eligible_account(db, "claim-b", now=NOW)
    group = Group(group_id=-1_000_000_123_456, username="shared_candidate", status="pending_join")
    db.add(group)
    await db.commit()
    return a, b, group


async def reserve(db, account_id, group, *, now=NOW):
    return await _service(db).reserve(
        account_id, group_id=group.id, telegram_group_id=group.group_id,
        group_username=group.username, source="auto_join", now=now,
    )


@pytest.mark.parametrize("status", ["joined", "pending", "leave_failed"])
async def test_live_membership_prevents_another_account_joining(test_db, status):
    a, b, group = await shared_fixture(test_db)
    test_db.add(GroupAccountMembership(
        group_id=group.id, telegram_group_id=group.group_id, account_id=a.id,
        status=status, joined_at=NOW, review_status="approved", ad_status="blocked",
    ))
    await test_db.commit()
    assert await occupied_group_ids(test_db, [group.id], NOW) == {group.id}
    with pytest.raises(JoinBudgetBlocked, match="already_joined_by_other_account|join_target_occupied"):
        await reserve(test_db, b.id, group)
    assert await test_db.scalar(select(func.count(AutoJoinAttempt.id))) == 0


async def test_reservation_blocks_other_account_and_release_returns_group_to_pool(test_db):
    a, b, group = await shared_fixture(test_db)
    first = await reserve(test_db, a.id, group)
    with pytest.raises(JoinBudgetBlocked, match="join_target_occupied"):
        await reserve(test_db, b.id, group)
    assert await occupied_group_ids(test_db, [group.id], NOW) == {group.id}
    service = AcquisitionAutomationService(test_db)
    assert await service._next_pending_join_group(account_id=b.id) is None
    await _service(test_db).release(first, reason="not_sent", now=NOW)
    assert await occupied_group_ids(test_db, [group.id], NOW) == set()
    assert (await reserve(test_db, b.id, group)).request_state == "reserved"


@pytest.mark.parametrize("unknown", [False, True])
async def test_confirmed_failure_is_reusable_but_unknown_outcome_remains_owned(test_db, unknown):
    a, b, group = await shared_fixture(test_db)
    service = _service(test_db)
    first = await reserve(test_db, a.id, group)
    await service.mark_sent(first, now=NOW)
    await service.finalize(first, status=DeliveryStatus.FAILED, reason="failed", outcome_unknown=unknown)
    if unknown:
        with pytest.raises(JoinBudgetBlocked, match="join_target_occupied"):
            await reserve(test_db, b.id, group, now=NOW + timedelta(days=1))
    else:
        assert (await reserve(test_db, b.id, group)).request_state == "reserved"


async def test_sent_pending_approval_stays_owned_past_reservation_expiry(test_db):
    a, b, group = await shared_fixture(test_db)
    first = await reserve(test_db, a.id, group)
    await _service(test_db).mark_sent(first, now=NOW)
    with pytest.raises(JoinBudgetBlocked, match="join_target_occupied"):
        await reserve(test_db, b.id, group, now=NOW + timedelta(days=1))


async def test_expired_unsent_reservation_can_be_claimed_by_another_account(test_db):
    a, b, group = await shared_fixture(test_db)
    first = await reserve(test_db, a.id, group)
    assert (await reserve(test_db, b.id, group, now=NOW + timedelta(minutes=11))).account_id == b.id
    with pytest.raises(JoinBudgetBlocked, match="join_reservation_expired"):
        await _service(test_db).mark_sent(first, now=NOW + timedelta(minutes=12))


async def test_telegram_id_and_username_aliases_share_the_same_claim(test_db):
    a, b, group = await shared_fixture(test_db)
    first = await _service(test_db).reserve(a.id, target_key="https://t.me/SHARED_CANDIDATE", source="auto_join", now=NOW)
    with pytest.raises(JoinBudgetBlocked, match="join_target_occupied"):
        await _service(test_db).reserve(b.id, telegram_group_id=123_456, source="auto_join", now=NOW)
    assert await occupied_group_ids(test_db, [group.id], NOW) == {group.id}
    await _service(test_db).release(first, reason="cancelled", now=NOW)
    assert (await reserve(test_db, b.id, group)).account_id == b.id


async def test_confirmed_join_then_leave_returns_group_to_pool(test_db):
    a, b, group = await shared_fixture(test_db)
    test_db.add_all([
        GroupAccountMembership(group_id=group.id, telegram_group_id=group.group_id,
            account_id=a.id, status="left", ad_status="blocked", left_at=NOW, joined_at=NOW - timedelta(days=1)),
        AutoJoinAttempt(account_id=a.id, group_id=group.id, telegram_group_id=group.group_id,
            status="success", request_state="sent", telegram_action_attempted=True,
            joined_at=NOW - timedelta(days=1), reconciliation_status="pending"),
    ])
    await test_db.commit()
    assert await occupied_group_ids(test_db, [group.id], NOW) == set()
    assert (await reserve(test_db, b.id, group)).account_id == b.id


async def test_send_boundary_rechecks_new_membership_from_another_account(test_db):
    a, b, group = await shared_fixture(test_db)
    first = await reserve(test_db, a.id, group)
    test_db.add(GroupAccountMembership(group_id=group.id, telegram_group_id=group.group_id,
        account_id=b.id, status="joined", joined_at=NOW))
    await test_db.commit()
    with pytest.raises(JoinBudgetBlocked, match="already_joined_by_other_account"):
        await _service(test_db).mark_sent(first, now=NOW)
    assert first.telegram_action_attempted is False
