"""Historical reconciler labels cannot release unproven Telegram write targets."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.group.models import Group
from app.modules.acquisition import qualification_engine as engine
from app.modules.acquisition.join_budget import JoinBudgetBlocked
from app.modules.acquisition.join_claims import occupied_group_ids
from app.modules.acquisition.models import AutoJoinAttempt
from tests.unit.modules.acquisition.test_join_budget import _eligible_account, _service

pytestmark = pytest.mark.asyncio


async def seed(db, monkeypatch, *, state="sent", status="pending", reconciliation="resolved"):
    now = datetime.utcnow()
    owner, _ = await _eligible_account(db, "legacy-fence-owner", now=now)
    other, _ = await _eligible_account(db, "legacy-fence-other", now=now)
    monkeypatch.setattr(engine.settings, "GROWTH_EFFICIENCY_ENABLED", True)
    monkeypatch.setattr(engine.settings, "GROWTH_EFFICIENCY_ACCOUNT_IDS", f"{owner.id},{other.id}")
    target = Group(group_id=-1002192235808, username="legacy_pending_target", status="pending_join")
    unrelated = Group(group_id=-1002192235809, username="unrelated_new_target", status="pending_join")
    db.add_all([target, unrelated])
    await db.flush()
    legacy = AutoJoinAttempt(
        account_id=owner.id, group_id=target.id, telegram_group_id=2192235808,
        group_username=target.username, target_key="username:legacy_pending_target",
        attempted_at=now-timedelta(days=12), request_sent_at=now-timedelta(days=12),
        status=status, request_state=state, telegram_action_attempted=True,
        reason="join_request_still_pending" if status == "pending" else "platform_denied",
        reconciliation_status=reconciliation, reconciliation_checked_at=now-timedelta(days=7),
        joined_at=None, sdk_entered_at=None, platform_responded_at=None,
    )
    db.add(legacy)
    await db.commit()
    return now, owner, other, target, unrelated, legacy


@pytest.mark.parametrize("actor", ["same", "other"])
@pytest.mark.parametrize("identity", ["username", "telegram", "group"])
async def test_legacy_resolved_pending_request_fences_target_aliases(test_db, monkeypatch, actor, identity):
    now, owner, other, target, _, legacy = await seed(test_db, monkeypatch)
    account = owner if actor == "same" else other
    identity_args = ({"target_key": "https://t.me/LEGACY_PENDING_TARGET/"} if identity == "username" else
                     {"telegram_group_id": -2192235808} if identity == "telegram" else
                     {"group_id": target.id})
    expected = "join_outcome_reconciliation_required" if actor == "same" else "join_target_occupied"
    with pytest.raises(JoinBudgetBlocked, match=expected):
        await _service(test_db).reserve(account.id, source="manual_group_link_join",
            require_auto_join_enabled=False, now=now, **identity_args)
    assert await occupied_group_ids(test_db, [target.id], now) == {target.id}
    assert await test_db.scalar(select(func.count(AutoJoinAttempt.id))) == 1
    await test_db.refresh(legacy)
    assert legacy.request_state == "sent" and legacy.sdk_entered_at is None
    assert legacy.platform_responded_at is None and legacy.reconciliation_status == "resolved"


@pytest.mark.parametrize("actor", ["same", "other"])
async def test_legacy_unproven_target_does_not_block_another_target(test_db, monkeypatch, actor):
    now, owner, other, _, target, legacy = await seed(test_db, monkeypatch)
    account = owner if actor == "same" else other
    service = _service(test_db)
    admitted = await service.reserve(account.id, group_id=target.id,
        telegram_group_id=target.group_id, group_username=target.username,
        source="auto_join", now=now)
    assert admitted.request_state == "reserved"
    await service.mark_sent(admitted, now=now)
    assert admitted.request_state == "sent" and admitted.sdk_entered_at is None
    await test_db.refresh(legacy)
    assert legacy.status == "pending" and legacy.request_state == "sent"
    assert legacy.sdk_entered_at is None and legacy.platform_responded_at is None


@pytest.mark.parametrize("reconciliation", ["resolved", "confirmed"])
async def test_unknown_write_never_released_by_terminal_reconciliation_label(
        test_db, monkeypatch, reconciliation):
    now, _, other, target, _, _ = await seed(test_db, monkeypatch,
        state="outcome_unknown", status="failed", reconciliation=reconciliation)
    with pytest.raises(JoinBudgetBlocked, match="join_target_occupied"):
        await _service(test_db).reserve(other.id, telegram_group_id=target.group_id,
            source="auto_join", now=now)


async def test_proven_failure_is_still_reusable_with_no_new_sdk_metrics(test_db, monkeypatch):
    now, owner, _, target, _, _ = await seed(test_db, monkeypatch, status="failed")
    admitted = await _service(test_db).reserve(owner.id, group_id=target.id,
        source="auto_join", now=now)
    assert admitted.request_state == "reserved"


@pytest.mark.parametrize("actor", ["same", "other"])
async def test_send_boundary_rechecks_a_restored_legacy_pending_fence(test_db, monkeypatch, actor):
    now, owner, other, target, _, legacy = await seed(test_db, monkeypatch, status="failed")
    account = owner if actor == "same" else other
    service = _service(test_db)
    admitted = await service.reserve(account.id, group_id=target.id,
        source="auto_join", now=now)
    legacy.status = "pending"
    legacy.reason = "join_request_still_pending"
    await test_db.commit()
    expected = "join_outcome_reconciliation_required" if actor == "same" else "join_target_occupied"
    with pytest.raises(JoinBudgetBlocked, match=expected):
        await service.mark_sent(admitted, now=now)
    assert admitted.request_state == "reserved"
    assert admitted.telegram_action_attempted is False and admitted.sdk_entered_at is None
