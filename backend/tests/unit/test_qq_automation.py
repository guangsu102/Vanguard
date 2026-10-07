from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.integrations.qq.client import OneBotAPIError
from app.modules.acquisition.models import AdCreative
from app.modules.qq.automation import QQAutomationService, day_bounds, next_scheduled_time
from app.modules.qq.models import (
    QQAdBinding,
    QQAdCampaign,
    QQAdSchedule,
    QQAutomationLog,
    QQBotConnection,
    QQCampaignTarget,
    QQJoinTask,
    QQManagedGroup,
)

NOW = datetime(2026, 10, 1, 4)
GROUP = "123456789"


class MemoryRedis:
    def __init__(self):
        self.values = {}

    async def set(self, key, value, *, nx, ex):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def eval(self, script, count, key, value):
        if self.values.get(key) == value:
            self.values.pop(key)
            return 1
        return 0


class FakeQQ:
    def __init__(self, *, joined=True, account_id="10001"):
        self.account_id = account_id
        self.groups = [{"group_id": int(GROUP), "group_name": "Test"}] if joined else []
        self.sent = []
        self.send_error = None
        self.muted_until = 0
        self.before_send = None
        self.closed = False

    async def get_login_info(self):
        return {"user_id": self.account_id, "nickname": "Test"}

    async def get_group_list(self):
        return self.groups

    async def get_group_member_info(self, group_number):
        if self.before_send:
            await self.before_send()
        return {"shut_up_timestamp": self.muted_until}

    async def send_group_segments(self, group_number, segments):
        self.sent.append((group_number, segments))
        if self.send_error:
            raise self.send_error
        return {"message_id": -1234}

    async def close(self):
        self.closed = True


class FakeBridge:
    def __init__(self, status="pending_approval"):
        self.status = status
        self.calls = []

    async def request_join(self, **payload):
        self.calls.append(payload)
        return {"status": self.status}

    async def close(self):
        pass


async def seed(db, *, mode="interval", wait=0):
    account = QQBotConnection(app_id="10001", automation_enabled=True)
    campaign = QQAdCampaign(
        name="QQ campaign",
        enabled=True,
        send_mode=mode,
        min_wait_after_join_minutes=wait,
        interval_minutes=1,
        max_sends_per_group_per_day=5,
    )
    creative = AdCreative(name="Shared with TG", content="广告 [CQ:at,qq=all]", enabled=True)
    db.add_all([account, campaign, creative])
    await db.flush()
    target = QQCampaignTarget(campaign_id=campaign.id, group_number=GROUP, verify_message="hello")
    binding = QQAdBinding(
        connection_id=account.id, campaign_id=campaign.id, creative_id=creative.id
    )
    db.add_all([target, binding])
    await db.commit()
    return account, campaign, creative, target, binding


def make_service(db, client, *, redis=None, bridge=None):
    return QQAutomationService(
        db, redis or MemoryRedis(), client_factory=lambda _: client, join_factory=lambda _: bridge
    )


@pytest.mark.asyncio
async def test_registered_group_without_remote_membership_does_not_receive_ad(test_db):
    account, *_ = await seed(test_db)
    test_db.add(QQManagedGroup(connection_id=account.id, group_openid=GROUP, status="active"))
    await test_db.commit()
    client = FakeQQ(joined=False)
    result = await make_service(test_db, client).tick(NOW)
    assert result["ads"] == 0 and not client.sent
    task = await test_db.scalar(select(QQJoinTask))
    assert task.status == "unsupported"
    assert task.attempt_count == 0
    assert "NapCat" in task.error_message


@pytest.mark.asyncio
async def test_bound_shared_creative_is_sent_and_recorded_once(test_db):
    account, campaign, creative, *_ = await seed(test_db, mode="after_join")
    client = FakeQQ()
    service = make_service(test_db, client)
    first = await service.tick(NOW)
    second = await service.tick(NOW + timedelta(hours=2))
    assert first["ads"] == 1 and second["ads"] == 0
    assert client.sent == [(GROUP, [{"type": "text", "data": {"text": creative.content}}])]
    log = await test_db.scalar(select(QQAutomationLog))
    assert (log.connection_id, log.campaign_id, log.creative_id) == (
        account.id,
        campaign.id,
        creative.id,
    )
    assert log.status == "succeeded" and log.provider_message_id == "-1234"
    schedule = await test_db.scalar(select(QQAdSchedule))
    assert schedule.status == "completed"


@pytest.mark.asyncio
async def test_uncertain_send_is_paused_and_not_retried(test_db):
    await seed(test_db)
    client = FakeQQ()
    client.send_error = OneBotAPIError("timeout", uncertain=True)
    service = make_service(test_db, client)
    await service.tick(NOW)
    await service.tick(NOW + timedelta(days=1))
    assert len(client.sent) == 1
    log = await test_db.scalar(select(QQAutomationLog))
    schedule = await test_db.scalar(select(QQAdSchedule))
    assert log.status == "unknown" and schedule.status == "paused"


@pytest.mark.asyncio
async def test_interrupted_send_is_recovered_without_reposting(test_db):
    account, campaign, *_ = await seed(test_db)
    client = FakeQQ()
    service = make_service(test_db, client)
    await service.sync_membership(account, client, NOW)
    await service.prepare_work(account, NOW)
    schedule = await test_db.scalar(select(QQAdSchedule))
    test_db.add(
        QQAutomationLog(
            connection_id=account.id,
            campaign_id=campaign.id,
            group_number=GROUP,
            operation_type="ad",
            operation_key=f"ad:{schedule.id}:{schedule.next_due_at.isoformat()}",
            payload_json=json.dumps({"schedule_id": schedule.id}),
            created_at=NOW - timedelta(minutes=3),
        )
    )
    await test_db.commit()
    await service.tick(NOW)
    assert not client.sent
    assert schedule.status == "paused"
    assert (await test_db.scalar(select(QQAutomationLog))).status == "unknown"


@pytest.mark.asyncio
async def test_bridge_joined_reply_requires_remote_membership_confirmation(test_db):
    await seed(test_db)
    client = FakeQQ(joined=False)
    bridge = FakeBridge(status="joined")
    service = make_service(test_db, client, bridge=bridge)
    await service.tick(NOW)
    await service.tick(NOW + timedelta(minutes=2))
    assert len(bridge.calls) == 1 and not client.sent
    assert (await test_db.scalar(select(QQJoinTask))).status == "pending_approval"
    client.groups = [{"group_id": int(GROUP)}]
    await service.tick(NOW + timedelta(minutes=3))
    assert len(client.sent) == 1
    assert (await test_db.scalar(select(QQJoinTask))).status == "joined"


@pytest.mark.asyncio
async def test_account_lock_prevents_overlapping_execution(test_db):
    account, *_ = await seed(test_db)
    client = FakeQQ()
    redis = MemoryRedis()
    redis.values[f"vanguard:qq:account:{account.id}"] = "another-worker"
    result = await make_service(test_db, client, redis=redis).tick(NOW)
    assert result["accounts"] == 0 and not client.sent
    assert redis.values[f"vanguard:qq:account:{account.id}"] == "another-worker"


@pytest.mark.asyncio
async def test_campaign_disabled_during_permission_read_prevents_send(test_db):
    _, campaign, *_ = await seed(test_db)
    client = FakeQQ()

    async def disable():
        campaign.enabled = False
        await test_db.commit()

    client.before_send = disable
    await make_service(test_db, client).tick(NOW)
    assert not client.sent
    assert await test_db.scalar(select(QQAutomationLog.id)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["quota", "campaign_window"])
async def test_changed_controls_are_checked_after_permission_read(test_db, change):
    account, campaign, *_ = await seed(test_db)
    client = FakeQQ()

    async def modify():
        if change == "quota":
            account.max_sends_per_day = 0
        else:
            campaign.start_at = NOW + timedelta(hours=1)
        await test_db.commit()

    client.before_send = modify
    await make_service(test_db, client).tick(NOW)
    assert not client.sent


@pytest.mark.asyncio
async def test_group_daily_cap_is_shared_across_qq_accounts(test_db):
    account, campaign, creative, *_ = await seed(test_db)
    campaign.max_sends_per_group_per_day = 1
    second_account = QQBotConnection(app_id="10002", automation_enabled=True)
    test_db.add(second_account)
    await test_db.flush()
    test_db.add(
        QQAdBinding(
            connection_id=second_account.id, campaign_id=campaign.id, creative_id=creative.id
        )
    )
    await test_db.commit()
    clients = {account.id: FakeQQ(), second_account.id: FakeQQ(account_id="10002")}
    service = QQAutomationService(
        test_db, MemoryRedis(), client_factory=lambda a: clients[a.id], join_factory=lambda _: None
    )
    result = await service.tick(NOW)
    assert result["ads"] == 1
    assert sum(len(c.sent) for c in clients.values()) == 1


@pytest.mark.asyncio
async def test_join_wait_and_departed_membership_prevent_ads(test_db):
    await seed(test_db, wait=60)
    client = FakeQQ()
    service = make_service(test_db, client)
    await service.tick(NOW)
    assert not client.sent
    client.groups = []
    await service.tick(NOW + timedelta(hours=2))
    assert not client.sent
    member = await test_db.scalar(select(QQManagedGroup))
    assert member.membership_status == "left"


def test_scheduled_delivery_and_day_bounds_use_campaign_timezone():
    campaign = QQAdCampaign(timezone="Asia/Shanghai", scheduled_times_json='["09:00", "18:00"]')
    assert next_scheduled_time(campaign, datetime(2026, 10, 1, 2)) == datetime(2026, 10, 1, 10)
    assert next_scheduled_time(campaign, datetime(2026, 10, 1, 11)) == datetime(2026, 10, 2, 1)
    assert day_bounds(datetime(2026, 9, 30, 17), "Asia/Shanghai") == (
        datetime(2026, 9, 30, 16),
        datetime(2026, 10, 1, 16),
    )
