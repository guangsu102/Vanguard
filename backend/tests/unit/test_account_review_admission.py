"""One account's review backlog must not consume another account's join slots."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.core.account.models import TelegramAccount
from app.core.group.models import Group, GroupAccountMembership
from app.modules.acquisition.execution_admission import execution_plan
from app.modules.acquisition.growth_admission import admission_plan, review_debt
from app.modules.acquisition.join_budget import is_hard_join_admission_blocker
from app.modules.acquisition.models import GroupQualificationAudit

NOW = datetime(2026, 10, 10)
pytestmark = pytest.mark.asyncio


async def add_member(db, account_id, *, audit=None):
    group = Group(group_id=100_000 + len(db.new) + account_id)
    # Flushed IDs also let subsequent fixture calls use distinct Telegram IDs.
    from sqlalchemy import func, select
    group.group_id += int(await db.scalar(select(func.count(Group.id))) or 0)
    db.add(group)
    await db.flush()
    member = GroupAccountMembership(
        group_id=group.id, telegram_group_id=group.group_id, account_id=account_id,
        status="joined", review_status="initial_pending", joined_at=NOW - timedelta(hours=1),
    )
    db.add(member)
    await db.flush()
    if audit is not None:
        db.add(GroupQualificationAudit(
            membership_id=member.id, membership_joined_at=member.joined_at,
            account_id=account_id, group_id=group.id, batch_id=f"review:{member.id}",
            policy_version="test", content_scope="text_profile", evidence_json="{}", **audit,
        ))
    await db.flush()
    return member


async def accounts(db):
    db.add_all([TelegramAccount(id=i, identifier=f"admission-{i}", session_name=f"admission-{i}")
                for i in (2, 33)])
    await db.flush()


async def test_old_account_backlog_does_not_block_new_account(test_db, monkeypatch):
    await accounts(test_db)
    for _ in range(16):
        await add_member(test_db, 2)
    monkeypatch.setattr("app.core.redis.get_redis", AsyncMock(side_effect=RuntimeError("offline")))
    usage = {key: {"used": 0} for key in (
        "survival_lent_day", "ad_day", "survival_day", "critical_day", "sync_day", "routine_day",
    )}
    usage["day"] = {"used": 0, "ttl_seconds": 86400}
    old = await admission_plan(test_db, 2, NOW, rpc={"state": "ready", "limits": {"day": 3240}, "usage": usage})
    new = await admission_plan(test_db, 33, NOW, rpc={"state": "ready", "limits": {"day": 3240}, "usage": usage})
    assert old["execution"]["pending"] == 16
    assert old["remaining"] == 0
    assert new["execution"]["scope"] == "account"
    assert new["execution"]["account_id"] == 33
    assert new["execution"]["pending"] == 0
    assert new["review_reads_due"] == 0
    assert new["remaining"] == 4


@pytest.mark.parametrize("audit,expected", [
    ({"state": "completed", "decision": "reject"}, 0),
    ({"state": "completed", "decision": "observe"}, 0),
    ({"state": "completed", "decision": "technical_wait"}, 0),
    ({"state": "completed", "decision": "technical_wait", "next_retry_at": NOW - timedelta(seconds=1)}, 1),
    ({"state": "completed", "decision": "observe", "next_retry_at": NOW + timedelta(seconds=30)}, 0),
    ({"state": "queued", "decision": "unknown", "next_retry_at": NOW + timedelta(seconds=30)}, 0),
    ({"state": "queued", "decision": "unknown"}, 1),
    ({"state": "running", "decision": "unknown", "next_retry_at": NOW + timedelta(minutes=5)}, 1),
])
async def test_only_due_or_running_reviews_occupy_execution_slots(test_db, audit, expected):
    await accounts(test_db)
    await add_member(test_db, 33, audit=audit)
    plan = await execution_plan(test_db, NOW, account_id=33)
    assert plan["pending"] == expected
    assert plan["remaining"] == 4 - expected


async def test_terminal_observation_has_no_phantom_read_debt(test_db, monkeypatch):
    await accounts(test_db)
    await add_member(test_db, 33, audit={"state": "completed", "decision": "observe"})
    monkeypatch.setattr("app.core.redis.get_redis", AsyncMock(side_effect=RuntimeError("offline")))
    assert await review_debt(test_db, 33, NOW, NOW + timedelta(days=1)) == (0, 0)


async def test_real_account_backlog_still_blocks_its_own_joins(test_db):
    await accounts(test_db)
    for _ in range(4):
        await add_member(test_db, 33)
    plan = await execution_plan(test_db, NOW, account_id=33)
    assert plan["pending"] == 4
    assert plan["remaining"] == 0
    assert plan["reason"] == "join_wait_execution_capacity"


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("join_wait_execution_capacity", False),
        ("telegram_read_budget", True),
        ("join_outcome_reconciliation_required", True),
        (None, False),
    ],
)
def test_execution_capacity_is_telemetry_not_a_hard_join_blocker(reason, expected):
    assert is_hard_join_admission_blocker(reason) is expected
