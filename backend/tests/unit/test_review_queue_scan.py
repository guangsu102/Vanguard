"""Real SQL review selection must reach affordable work behind budget waits."""

import importlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import Base
from app.core.group.models import Group, GroupAccountMembership
from app.core.settings_models import SystemSetting
from app.modules.acquisition import qualification_queue as queue
from app.modules.acquisition import qualification_service as service
from app.modules.acquisition.models import GroupQualificationAudit
from app.modules.acquisition.qualification_events import request
from app.modules.acquisition.review_waits import record
from tests.unit.test_qualification_service import setup

pytestmark = pytest.mark.asyncio(loop_scope="module")


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


@pytest_asyncio.fixture(loop_scope="module")
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    for name in (
        "app.core.account.models", "app.core.group.models", "app.core.keyword.models",
        "app.core.user.models", "app.core.campaign.models", "app.core.worker_status",
        "app.core.settings_models", "app.api.broadcasts", "app.modules.guardian.models",
        "app.modules.acquisition.models", "app.modules.private_chat.models", "app.modules.qq.models",
        "app.modules.owned_group.models", "app.modules.owned_group.models_extra",
        "app.modules.owned_group.messaging_models", "app.integrations.xboard.models",
    ):
        importlib.import_module(name)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def add_review(db, member, offset, *, decision="observe", fresh=False):
    group = Group(id=100+offset, group_id=-1002000000000-offset)
    joined = GroupAccountMembership(id=200+offset, group_id=group.id,
        telegram_group_id=group.group_id, account_id=member.account_id,
        status="joined", joined_at=member.joined_at,
        review_status="initial_pending" if fresh else "review_2h", ad_status="blocked")
    row = GroupQualificationAudit(batch_id=f"scan-{offset}", membership_id=joined.id,
        account_id=member.account_id, group_id=group.id, policy_version=service.POLICY_VERSION,
        content_scope="text_profile", membership_joined_at=joined.joined_at,
        state="queued", decision="unknown" if fresh else decision,
        checked_at=None if fresh else utcnow(), next_retry_at=utcnow())
    db.add_all([group, joined, row])
    await db.flush()
    return group, joined, row


def mock_preflight(monkeypatch):
    monkeypatch.setattr(service, "ensure_membership_reviews", AsyncMock())
    monkeypatch.setattr("app.modules.acquisition.qualification_identity_refresh.queue_identity_refreshes", AsyncMock())
    # The real dispatcher may reconsider future local-budget waits after a
    # one-read wake-up probe. No Telegram or shared Redis is used by these tests.
    monkeypatch.setattr("app.core.account.rpc_governor.check_read_ready", AsyncMock(return_value={}))


def install_assessor(monkeypatch):
    seen = []

    async def assess(actor, account_id, group, *, row):
        seen.append(row.id)
        row.state, row.next_retry_at = "completed", None
        return SimpleNamespace(passed=False, reason="assessed")

    monkeypatch.setattr(service, "assess", assess)
    return seen


async def test_eight_unaffordable_waits_cannot_hide_two_sixteen_read_renewals(db, monkeypatch):
    _, _, member, renewal = await setup(db)
    now = utcnow()
    previous = json.loads(renewal.evidence_json)
    previous["advertising_audit"] = {"ad_allowed": True}
    renewal.evidence_json = json.dumps(previous)
    renewal.state, renewal.next_retry_at = "queued", now
    await request(db, member, "gap")
    blocked = []
    for offset in range(8):
        _, _, row = await add_review(db, member, offset)
        row.next_retry_at = now+timedelta(hours=10)
        await record(db, row.id, "telegram_read_budget", 26, row.next_retry_at, now)
        blocked.append(row)
    _, second_member, second = await add_review(db, member, 20, decision="allowed")
    second_member.review_status, second_member.ad_status = "approved", "active"
    second.evidence_json = json.dumps({**previous, "group_id":second.group_id,
        "telegram_group_id":second_member.telegram_group_id})
    await request(db, second_member, "gap")
    await db.commit()
    mock_preflight(monkeypatch)
    monkeypatch.setattr("app.core.account.read_work.review_required", AsyncMock(return_value=26))
    calls = []

    async def read_wait(db, account_id, now, *, purpose, minimum_reads, **options):
        calls.append((purpose, minimum_reads))
        if purpose == "ad_qualification_refresh":
            assert minimum_reads == 16
            return None
        assert minimum_reads == 26
        return "telegram_read_budget", now+timedelta(hours=10)

    monkeypatch.setattr("app.core.account.read_schedule.read_wait", read_wait)
    seen = install_assessor(monkeypatch)
    result = await service._run_reviews(SimpleNamespace(db=db), account_id=2, limit=2)
    assert result["processed"] == 2
    assert set(seen) == {renewal.id, second.id}
    assert len(calls) <= 8
    assert calls[:2] == [("group_qualification",26),("ad_qualification_refresh",16)]
    assert all(row.attempts == 0 for row in blocked)
    successful = await queue.cursor(db,2)
    assert successful["initial_id"] == 0
    assert successful["renewal_id"] in seen


async def test_scan_progress_survives_quantum_cap_and_revisits_when_budget_recovers(db, monkeypatch):
    _, _, member, first = await setup(db, decision="observe")
    first.state, first.next_retry_at = "queued", utcnow()
    rows = [first]
    for offset in range(17):
        _, _, row = await add_review(db,member,offset)
        rows.append(row)
    target = rows[-1]
    await db.commit()
    mock_preflight(monkeypatch)
    affordable = False

    async def required(account_id,group_id,scope,**options):
        return 16 if group_id == target.group_id else 26

    monkeypatch.setattr("app.core.account.read_work.review_required", required)
    attempted = []

    async def read_wait(db,account_id,now,*,minimum_reads,work_group_id,**options):
        attempted.append(work_group_id)
        if minimum_reads <= 18 or affordable:
            return None
        return "telegram_read_budget",now+timedelta(hours=10)

    monkeypatch.setattr("app.core.account.read_schedule.read_wait", read_wait)
    seen = install_assessor(monkeypatch)
    original = await queue.cursor(db,2)
    for _ in range(5):
        result = await service._run_reviews(SimpleNamespace(db=db),account_id=2,limit=1)
        if seen:
            break
        assert result["processed"] == 0
        assert await queue.cursor(db,2) == original
    assert seen == [target.id]
    assert len(set(attempted)) == len(rows)
    assert len(attempted) == len(rows)
    # A persisted position is one bounded setting, independent of backlog size.
    assert await db.scalar(select(func.count()).select_from(SystemSetting).where(
        SystemSetting.key.like("qualification.review_scan.%"))) == 1
    affordable = True
    for _ in range(len(rows)):
        await service._run_reviews(SimpleNamespace(db=db),account_id=2,limit=1)
    assert set(seen) == {row.id for row in rows}
    assert all(row.attempts == 1 for row in rows)


async def test_successful_claim_family_cycle_is_preserved_with_recurring_renewal(db, monkeypatch):
    _, _, member, renewal = await setup(db)
    initial, retries = [], []
    for offset in range(3):
        _, _, fresh = await add_review(db,member,offset,fresh=True)
        _, _, retry = await add_review(db,member,20+offset)
        initial.append(fresh)
        retries.append(retry)
    await db.commit()
    mock_preflight(monkeypatch)
    monkeypatch.setattr("app.core.account.read_work.review_required", AsyncMock(return_value=26))
    monkeypatch.setattr("app.core.account.read_schedule.read_wait", AsyncMock(return_value=None))
    seen = install_assessor(monkeypatch)
    families = []
    for _ in range(9):
        renewal.state, renewal.next_retry_at = "queued", utcnow()
        await db.commit()
        assert (await service._run_reviews(SimpleNamespace(db=db),account_id=2,limit=1))["processed"] == 1
        families.append((await queue.cursor(db,2))["last_family"])
    assert families == [2,1,0]*3
    assert set(seen) == {renewal.id,*(row.id for row in initial),*(row.id for row in retries)}


async def test_scan_and_claim_positions_rollback_without_overwriting_success_cursor(db):
    _, _, _, row = await setup(db)
    await queue.claimed(db,2,row.id,0)
    await db.commit()
    original = await queue.cursor(db,2)
    await queue.scanned(db,2,row.id+30,1)
    assert (await queue.scan_cursor(db,2))["last_family"] == original["last_family"]
    assert await queue.cursor(db,2) == original
    await db.rollback()
    assert await queue.cursor(db,2) == original
    assert await db.get(SystemSetting,queue.scan_key(2)) is None
    assert await queue.scan_cursor(db,3) == await queue.cursor(db,3)
