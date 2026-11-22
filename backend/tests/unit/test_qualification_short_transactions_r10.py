"""Real-session boundaries and concurrent scope changes around offline TG reads."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.models import TelegramAccount
from app.core.account.telegram_execution import TelegramExecutionError
from app.core.group.models import GroupAccountMembership
from app.core.settings_models import SystemSetting
from app.modules.acquisition import qualification_actions as actions
from app.modules.acquisition.models import GroupQualificationAudit
from tests.unit.test_qualification_actions import Client
from tests.unit.test_qualification_service import setup


class OfflineClient(Client):
    def __init__(self, db, group, *, during_read=None):
        super().__init__()
        self.db = db
        self.entity.id = group.group_id
        self.during_read = during_read
        self.reads = 0
        self.checked_out = 0
        self.sync_engine = db.bind.sync_engine
        event.listen(self.sync_engine, "checkout", self.checkout)
        event.listen(self.sync_engine, "checkin", self.checkin)

    def checkout(self, *_args):
        self.checked_out += 1

    def checkin(self, *_args):
        self.checked_out -= 1

    def close(self):
        event.remove(self.sync_engine, "checkout", self.checkout)
        event.remove(self.sync_engine, "checkin", self.checkin)

    async def before_read(self):
        assert not self.db.in_transaction(), "business transaction crossed a Telegram read"
        assert self.checked_out == 0, "business connection remained checked out"
        self.reads += 1
        if self.during_read is not None:
            callback, self.during_read = self.during_read, None
            await callback()

    async def get_entity(self, target):
        await self.before_read()
        return await super().get_entity(target)

    async def get_permissions(self, entity, who):
        await self.before_read()
        return await super().get_permissions(entity, who)

    async def __call__(self, request):
        await self.before_read()
        return await super().__call__(request)


@pytest.mark.asyncio
async def test_real_refresh_returns_connection_before_all_network_reads(test_db):
    account, group, member, row = await setup(test_db)
    original = json.loads(row.evidence_json)
    client = OfflineClient(test_db, group)
    try:
        await actions.refresh_live_authorization(
            test_db, client, account.id, group.group_id, permissions_only=True,
        )
    finally:
        client.close()
    assert client.reads == 3
    assert not test_db.in_transaction()
    saved = json.loads(row.evidence_json)
    assert saved["evidence"] == original["evidence"]
    assert saved["permissions"]["member"] is True
    assert member.review_status == "approved"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["claim", "membership", "profile", "event", "policy", "new_reject"])
async def test_refresh_never_overwrites_a_scope_changed_during_reads(test_db, change):
    account, group, member, row = await setup(test_db)
    expected_evidence = row.evidence_json
    audit_id, member_id, account_id = row.id, member.id, account.id

    async def concurrent_change():
        async with AsyncSession(test_db.bind, expire_on_commit=False) as concurrent:
            if change == "claim":
                await concurrent.execute(update(GroupQualificationAudit).where(
                    GroupQualificationAudit.id == audit_id,
                ).values(attempts=77, state="queued"))
            elif change == "membership":
                await concurrent.execute(update(GroupAccountMembership).where(
                    GroupAccountMembership.id == member_id,
                ).values(status="left", left_at=datetime.utcnow(), ad_status="blocked"))
            elif change == "profile":
                await concurrent.execute(update(TelegramAccount).where(
                    TelegramAccount.id == account_id,
                ).values(profile_bio="changed during read"))
            elif change == "event":
                from app.modules.acquisition.qualification_events import request

                current = await concurrent.get(GroupAccountMembership, member_id)
                await request(concurrent, current, "rules", message_ids=[999])
            elif change == "policy":
                from app.modules.acquisition.qualification_service import SETTING_KEY

                setting = await concurrent.get(SystemSetting, SETTING_KEY)
                value = json.loads(setting.value)
                value["execute_exits"] = not value["execute_exits"]
                setting.value = json.dumps(value)
            else:
                concurrent.add(GroupQualificationAudit(
                    batch_id="new-negative-verdict", membership_id=member_id, account_id=account_id,
                    group_id=group.id, membership_joined_at=member.joined_at,
                    policy_version=row.policy_version, content_scope=row.content_scope,
                    state="completed", decision="reject", evidence_json='{"new_rejection":true}',
                ))
            await concurrent.commit()

    client = OfflineClient(test_db, group, during_read=concurrent_change)
    try:
        with pytest.raises(TelegramExecutionError, match="qualification_refresh_scope_changed"):
            await actions.refresh_live_authorization(
                test_db, client, account.id, group.group_id, permissions_only=True,
            )
    finally:
        client.close()
    assert row.evidence_json == expected_evidence
    assert not test_db.in_transaction()
    if change == "claim":
        assert row.attempts == 77 and row.state == "queued"
    if change == "membership":
        assert member.status == "left"


@pytest.mark.asyncio
@pytest.mark.parametrize("flushed", [False, True])
async def test_refresh_does_not_commit_an_unrelated_orm_write(test_db, flushed):
    account, group, _, _ = await setup(test_db)
    unrelated = SystemSetting(key="operator.uncommitted", value="must remain pending")
    test_db.add(unrelated)
    if flushed:
        await test_db.flush()
    client = OfflineClient(test_db, group)
    try:
        with pytest.raises(TelegramExecutionError, match="qualification_transaction_pending"):
            await actions.refresh_live_authorization(
                test_db, client, account.id, group.group_id, permissions_only=True,
            )
    finally:
        client.close()
    assert client.reads == 0
    await test_db.rollback()
    assert await test_db.get(SystemSetting, "operator.uncommitted") is None


@pytest.mark.asyncio
async def test_refresh_does_not_commit_a_flushed_unowned_field_on_the_same_audit(test_db):
    account, group, _, row = await setup(test_db)
    row.reason = "caller has not committed this verdict"
    await test_db.flush()
    client = OfflineClient(test_db, group)
    try:
        with pytest.raises(TelegramExecutionError, match="qualification_transaction_pending"):
            await actions.refresh_live_authorization(
                test_db, client, account.id, group.group_id, permissions_only=True,
            )
    finally:
        client.close()
    assert client.reads == 0
    await test_db.rollback()
    assert (await test_db.get(GroupQualificationAudit, 1)).reason is None


@pytest.mark.asyncio
async def test_permission_rejection_keeps_evidence_and_original_expiry(test_db):
    account, group, _, row = await setup(test_db)
    original, expiry = row.evidence_json, row.expires_at
    client = OfflineClient(test_db, group)
    client.full.slowmode_next_send_date = datetime.utcnow() + timedelta(minutes=5)
    try:
        with pytest.raises(TelegramExecutionError, match="qualification_slowmode_wait"):
            await actions.refresh_live_authorization(
                test_db, client, account.id, group.group_id, permissions_only=True,
            )
    finally:
        client.close()
    assert row.evidence_json == original and row.expires_at == expiry
    assert not test_db.in_transaction()


@pytest.mark.asyncio
async def test_renewal_releases_transaction_before_pool_and_retains_new_event(test_db, monkeypatch):
    from app.modules.acquisition.qualification_events import pending, request
    from app.modules.acquisition.qualification_renewal import try_light_renewal
    from tests.unit.test_qualification_light_renewal import fixture

    service, account, group, member, row, previous = await fixture(test_db)
    await request(test_db, member, "membership")
    await test_db.commit()

    async def acquire(*_args, **_kwargs):
        assert not test_db.in_transaction()
        return SimpleNamespace(client=SimpleNamespace())

    service.account_pool.acquire_by_id.side_effect = acquire

    async def refreshed(*_args, **_kwargs):
        assert not test_db.in_transaction()
        async with AsyncSession(test_db.bind, expire_on_commit=False) as concurrent:
            current = await concurrent.get(GroupAccountMembership, member.id)
            await request(concurrent, current, "rules", message_ids=[888])
            await concurrent.commit()

    monkeypatch.setattr(actions, "refresh_live_authorization", refreshed)
    result = await try_light_renewal(service, account, group, member, row, previous, force_refresh=False)
    assert not result.passed and result.reason == "qualification_refresh_scope_changed"
    assert (await pending(test_db, member))["message_ids"] == [888]
    assert "event_reconciled_at" not in json.loads(row.evidence_json)
    service.account_pool.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_renewal_rejects_new_claim_during_acquire_before_telegram_reads(test_db):
    from app.modules.acquisition.qualification_events import request
    from app.modules.acquisition.qualification_renewal import try_light_renewal
    from tests.unit.test_qualification_light_renewal import fixture

    service, account, group, member, row, previous = await fixture(test_db)
    await request(test_db, member, "membership")
    await test_db.commit()
    audit_id = row.id
    client = OfflineClient(test_db, group)

    async def acquire(*_args, **_kwargs):
        assert not test_db.in_transaction()
        async with AsyncSession(test_db.bind, expire_on_commit=False) as concurrent:
            await concurrent.execute(update(GroupQualificationAudit).where(
                GroupQualificationAudit.id == audit_id,
            ).values(state="queued", attempts=77))
            await concurrent.commit()
        return SimpleNamespace(client=client)

    service.account_pool.acquire_by_id.side_effect = acquire
    try:
        result = await try_light_renewal(
            service, account, group, member, row, previous, force_refresh=False,
        )
    finally:
        client.close()
    assert not result.passed and result.reason == "qualification_refresh_scope_changed"
    assert client.reads == 0
    assert row.state == "queued" and row.attempts == 77
    assert "event_reconciled_at" not in json.loads(row.evidence_json)
    service.account_pool.release.assert_awaited_once()
