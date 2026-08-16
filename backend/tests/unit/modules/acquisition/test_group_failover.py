from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.core.account.models import (
    AccountOperationConfig,
    AccountRiskLevel,
    AccountStatus,
    AccountType,
    TelegramAccount,
)
from app.core.group.models import Group, GroupAccountMembership
from app.modules.acquisition.automation import AcquisitionAutomationService, JoinedGroupAuditResult
from app.modules.acquisition.failover import GroupFailoverService
from app.modules.acquisition.models import (
    AccountAdBinding,
    AdCampaign,
    GroupFailoverStatus,
    GroupFailoverTask,
)

pytestmark = pytest.mark.asyncio


async def _add_account(
    db,
    *,
    identifier: str,
    status: AccountStatus,
    risk_reason: str | None = None,
) -> TelegramAccount:
    account = TelegramAccount(
        identifier=identifier,
        session_name=identifier,
        account_type=AccountType.PROMOTER,
        status=status,
        risk_level=(
            AccountRiskLevel.QUARANTINED.value
            if risk_reason == "account_banned"
            else AccountRiskLevel.NORMAL.value
        ),
        risk_reason=risk_reason,
        is_active=True,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return account


async def _add_group_membership(
    db,
    account: TelegramAccount,
    *,
    telegram_group_id: int,
    username: str | None,
) -> tuple[Group, GroupAccountMembership]:
    group = Group(
        group_id=telegram_group_id,
        title=f"group-{telegram_group_id}",
        username=username,
        status="active",
    )
    db.add(group)
    await db.commit()
    await db.refresh(group)
    membership = GroupAccountMembership(
        group_id=group.id,
        telegram_group_id=group.group_id,
        account_id=account.id,
        status="joined",
        join_method="auto_keyword_search",
    )
    db.add(membership)
    await db.commit()
    await db.refresh(membership)
    return group, membership


async def test_failover_discovery_is_idempotent_and_private_groups_require_manual_work(test_db):
    source = await _add_account(
        test_db,
        identifier="banned-source",
        status=AccountStatus.BANNED,
        risk_reason="account_banned",
    )
    _public_group, public_membership = await _add_group_membership(
        test_db,
        source,
        telegram_group_id=10001,
        username="recoverable_group",
    )
    _private_group, private_membership = await _add_group_membership(
        test_db,
        source,
        telegram_group_id=10002,
        username=None,
    )
    automation = AcquisitionAutomationService(test_db, account_pool=AsyncMock())
    service = GroupFailoverService(test_db, automation)

    dry_run = await service.run(max_tasks=10, dry_run=True)
    assert dry_run["created"] == 0
    assert dry_run["processed"] == 2
    assert (await test_db.execute(select(GroupFailoverTask))).scalars().all() == []

    result = await service.run(max_tasks=10)
    assert result["created"] == 2
    tasks = (
        (
            await test_db.execute(
                select(GroupFailoverTask).order_by(GroupFailoverTask.telegram_group_id)
            )
        )
        .scalars()
        .all()
    )
    assert [task.status for task in tasks] == [
        GroupFailoverStatus.QUEUED.value,
        GroupFailoverStatus.MANUAL_REQUIRED.value,
    ]
    assert tasks[0].reason == "no_eligible_target_account"
    assert tasks[0].next_retry_at is not None
    assert tasks[1].reason == "public_username_required"

    await test_db.refresh(public_membership)
    await test_db.refresh(private_membership)
    assert public_membership.status == "account_lost"
    assert private_membership.status == "account_lost"

    second = await service.run(max_tasks=10)
    assert second["created"] == 0
    assert len((await test_db.execute(select(GroupFailoverTask))).scalars().all()) == 2


async def test_failover_assigns_healthy_account_and_resets_membership_warmup(test_db):
    source = await _add_account(
        test_db,
        identifier="banned-source-2",
        status=AccountStatus.BANNED,
        risk_reason="account_banned",
    )
    group, source_membership = await _add_group_membership(
        test_db,
        source,
        telegram_group_id=20001,
        username="valuable_group",
    )
    target = await _add_account(
        test_db,
        identifier="healthy-target",
        status=AccountStatus.ONLINE,
    )
    test_db.add(
        AccountOperationConfig(
            account_id=target.id,
            enabled=True,
            auto_join_enabled=True,
            auto_ads_enabled=True,
            max_groups_per_day=10,
            max_groups_total=100,
        )
    )
    await test_db.commit()

    automation = AcquisitionAutomationService(test_db, account_pool=AsyncMock())
    automation._join_group = AsyncMock()
    automation._evaluate_joined_group = AsyncMock(
        return_value=JoinedGroupAuditResult(
            passed=True,
            can_send_messages=True,
            should_leave=False,
            ad_allowed=True,
        )
    )
    automation._record_join_attempt = AsyncMock()
    automation._sync_group_ad_policy_from_audit = AsyncMock()
    automation.group_manager.update_group = AsyncMock()
    automation._schedule_next_join = MagicMock()
    service = GroupFailoverService(test_db, automation)

    result = await service.run(max_tasks=1)
    assert result["succeeded"] == 1
    automation._join_group.assert_awaited_once()

    task = (await test_db.execute(select(GroupFailoverTask))).scalar_one()
    assert task.status == GroupFailoverStatus.SUCCEEDED.value
    assert task.target_account_id == target.id
    assert task.completed_at is not None

    target_membership = (
        await test_db.execute(
            select(GroupAccountMembership).where(
                GroupAccountMembership.group_id == group.id,
                GroupAccountMembership.account_id == target.id,
            )
        )
    ).scalar_one()
    assert target_membership.status == "joined"
    assert target_membership.join_method == "account_failover"
    assert target_membership.warmup_status == "joined_pending_test"
    assert target_membership.ad_status == "warming"
    assert target_membership.first_ad_allowed_at is not None

    await test_db.refresh(source_membership)
    assert source_membership.status == "account_lost"


async def test_existing_ad_capable_membership_reuses_and_enables_binding(test_db):
    source = await _add_account(
        test_db,
        identifier="banned-source-3",
        status=AccountStatus.BANNED,
        risk_reason="account_banned",
    )
    group, source_membership = await _add_group_membership(
        test_db,
        source,
        telegram_group_id=30001,
        username="already_covered_group",
    )
    target = await _add_account(
        test_db,
        identifier="healthy-existing-target",
        status=AccountStatus.ONLINE,
    )
    test_db.add_all(
        [
            AccountOperationConfig(
                account_id=target.id,
                enabled=True,
                auto_join_enabled=False,
                auto_ads_enabled=True,
            ),
            GroupAccountMembership(
                group_id=group.id,
                telegram_group_id=group.group_id,
                account_id=target.id,
                status="joined",
                join_method="manual",
            ),
        ]
    )
    campaign = AdCampaign(name="failover-binding-campaign", enabled=True, status="active")
    test_db.add(campaign)
    await test_db.commit()
    await test_db.refresh(campaign)

    source_binding = AccountAdBinding(
        account_id=source.id,
        ad_campaign_id=campaign.id,
        enabled=True,
        priority=10,
    )
    target_binding = AccountAdBinding(
        account_id=target.id,
        ad_campaign_id=campaign.id,
        enabled=False,
        priority=5,
    )
    test_db.add_all([source_binding, target_binding])
    await test_db.commit()

    automation = AcquisitionAutomationService(test_db, account_pool=AsyncMock())
    service = GroupFailoverService(test_db, automation)
    result = await service.run(max_tasks=1)

    assert result["created"] == 1
    task = (await test_db.execute(select(GroupFailoverTask))).scalar_one()
    assert task.status == GroupFailoverStatus.SUCCEEDED.value
    assert task.reason == "already_covered"
    assert task.target_account_id == target.id
    await test_db.refresh(target_binding)
    await test_db.refresh(source_membership)
    assert target_binding.enabled is True
    assert source_membership.status == "account_lost"


async def test_selected_target_accounts_are_balanced_across_groups(test_db):
    source = await _add_account(
        test_db,
        identifier="selected-source",
        status=AccountStatus.BANNED,
        risk_reason="account_banned",
    )
    for group_id in range(3):
        await _add_group_membership(
            test_db,
            source,
            telegram_group_id=50001 + group_id,
            username=f"selected_group_{group_id}",
        )

    targets = []
    for target_id in range(3):
        target = await _add_account(
            test_db,
            identifier=f"selected-target-{target_id}",
            status=AccountStatus.ONLINE,
        )
        test_db.add(
            AccountOperationConfig(
                account_id=target.id,
                enabled=True,
                auto_join_enabled=True,
                auto_ads_enabled=True,
                max_groups_per_day=10,
                max_groups_total=100,
            )
        )
        targets.append(target)
    await test_db.commit()

    automation = AcquisitionAutomationService(test_db, account_pool=AsyncMock())
    automation._join_group = AsyncMock()
    automation._evaluate_joined_group = AsyncMock(
        return_value=JoinedGroupAuditResult(
            passed=True,
            can_send_messages=True,
            should_leave=False,
            ad_allowed=True,
        )
    )
    automation._record_join_attempt = AsyncMock()
    automation._sync_group_ad_policy_from_audit = AsyncMock()
    automation.group_manager.update_group = AsyncMock()
    automation._schedule_next_join = MagicMock()
    service = GroupFailoverService(test_db, automation)

    result = await service.run(
        max_tasks=3,
        target_account_ids=[target.id for target in targets],
    )

    assert result["succeeded"] == 3
    tasks = (
        await test_db.execute(
            select(GroupFailoverTask).order_by(GroupFailoverTask.telegram_group_id)
        )
    ).scalars().all()
    assert len(tasks) == 3
    assert {task.target_account_id for task in tasks} == {target.id for target in targets}
    assert automation._join_group.await_count == 3


async def test_stale_joining_task_is_requeued(test_db):
    source = await _add_account(
        test_db,
        identifier="banned-source-4",
        status=AccountStatus.BANNED,
        risk_reason="account_banned",
    )
    group, source_membership = await _add_group_membership(
        test_db,
        source,
        telegram_group_id=40001,
        username="stale_claim_group",
    )
    source_membership.status = "account_lost"
    task = GroupFailoverTask(
        source_membership_id=source_membership.id,
        source_account_id=source.id,
        group_id=group.id,
        telegram_group_id=group.group_id,
        status=GroupFailoverStatus.JOINING.value,
        reason="target_assigned",
        attempt_count=1,
        last_attempt_at=datetime.utcnow() - timedelta(hours=1),
    )
    test_db.add(task)
    await test_db.commit()

    automation = AcquisitionAutomationService(test_db, account_pool=AsyncMock())
    service = GroupFailoverService(test_db, automation)
    await service.run(max_tasks=1, dry_run=True)
    await test_db.refresh(task)
    assert task.status == GroupFailoverStatus.JOINING.value

    recovered = await service._recover_stale_claims(datetime.utcnow())

    assert recovered == 1
    await test_db.refresh(task)
    assert task.status == GroupFailoverStatus.RETRY.value
    assert task.reason == "stale_claim_recovered"
    assert task.target_account_id is None
    assert task.next_retry_at is not None
