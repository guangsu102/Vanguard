"""Tests for the read-only multi-account resource search workflow."""

import importlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from sqlalchemy import func, select

from app.api.resource_search import ResourceSearchCreateRequest, _utc_iso
from app.celery import celery_app
from app.core.account.models import AccountStatus, AccountType, TelegramAccount
from app.core.account.operation_lease import AccountOperationLeaseHandle
from app.core.account.risk_guard import RiskDecision
from app.core.group.models import Group
from app.core.security import require_admin
from app.modules.acquisition.models import (
    AutoJoinAttempt,
    ResourceSearchAccountTask,
    ResourceSearchResult,
    ResourceSearchRun,
    ResourceSearchStatus,
)
from app.modules.acquisition.resource_search import (
    ResourceObservation,
    ResourceSearchService,
    ResourceSearchSuperseded,
    merge_resource_observations,
    reconcile_stale_resource_search_runs,
)
from app.modules.acquisition.search.group_finder import DiscoveredGroup
from scripts.apply_sql_migrations import DEFAULT_MIGRATIONS, _split_sql_statements

resource_search_api = importlib.import_module("app.api.resource_search")


def test_resource_search_sql_migration_is_registered_and_parseable():
    migration_name = "040_add_resource_search.sql"
    migration_path = Path(__file__).parents[4] / "migrations" / migration_name

    assert migration_name in DEFAULT_MIGRATIONS
    assert migration_path.is_file()
    statements = _split_sql_statements(migration_path.read_text(encoding="utf-8"))
    assert any(
        "CREATE TABLE IF NOT EXISTS acquisition_resource_search_run" in item
        for item in statements
    )
    assert any(
        "CREATE TABLE IF NOT EXISTS acquisition_resource_search_result" in item
        for item in statements
    )
    hardening_name = "041_harden_resource_search.sql"
    hardening_path = Path(__file__).parents[4] / "migrations" / hardening_name
    assert hardening_name in DEFAULT_MIGRATIONS
    assert hardening_path.is_file()
    hardening_statements = _split_sql_statements(
        hardening_path.read_text(encoding="utf-8")
    )
    assert any("ADD COLUMN IF NOT EXISTS heartbeat_at" in item for item in hardening_statements)


async def _create_account(test_db, identifier: str) -> TelegramAccount:
    account = TelegramAccount(
        identifier=identifier,
        display_name=identifier,
        session_name=identifier,
        session_string=f"session-{identifier}",
        status=AccountStatus.ONLINE,
        account_type=AccountType.PROMOTER,
        is_active=True,
    )
    test_db.add(account)
    await test_db.flush()
    return account


async def _create_run(test_db, accounts: list[TelegramAccount]) -> ResourceSearchRun:
    run = ResourceSearchRun(
        keywords_json=json.dumps(["VPN", "机场"], ensure_ascii=False),
        account_ids_json=json.dumps([account.id for account in accounts]),
        max_results_per_keyword=20,
        status=ResourceSearchStatus.QUEUED.value,
        total_accounts=len(accounts),
    )
    test_db.add(run)
    await test_db.flush()
    for account in accounts:
        test_db.add(
            ResourceSearchAccountTask(
                run_id=run.id,
                account_id=account.id,
                account_identifier=account.identifier,
            )
        )
    await test_db.commit()
    return run


class FakeGroupFinder:
    async def search_by_keyword(
        self,
        keyword: str,
        limit: int,
        account_id: int,
        raise_errors: bool,
        operation_lease=None,
    ) -> list[DiscoveredGroup]:
        assert limit == 20
        assert raise_errors is True
        assert operation_lease is not None
        return [
            DiscoveredGroup(
                group_id=987654,
                title="跨境资源交流群",
                username="Global_Resource_Hub",
                member_count=1000 + account_id,
                is_private=False,
                source_keyword=keyword,
            )
        ]


class PartiallyFailingFinder(FakeGroupFinder):
    def __init__(self, failing_account_id: int):
        self.failing_account_id = failing_account_id

    async def search_by_keyword(self, **kwargs):
        if kwargs["account_id"] == self.failing_account_id:
            raise RuntimeError("search unavailable")
        return await super().search_by_keyword(**kwargs)


class MixedIdentityFinder(FakeGroupFinder):
    async def search_by_keyword(self, keyword: str, **kwargs):
        assert kwargs["operation_lease"] is not None
        return [
            DiscoveredGroup(
                group_id=321 if keyword == "机场" else None,
                title="Mixed Resource",
                username="mixed_resource",
                member_count=200,
                is_private=False,
                source_keyword=keyword,
            )
        ]


class FakeLeaseManager:
    def __init__(self):
        self.refresh_calls = 0

    async def acquire(self, account_id: int, *, owner: str, ttl_seconds: int):
        return AccountOperationLeaseHandle(
            account_id=account_id,
            key=f"lease:{account_id}",
            token=owner,
            owner=owner,
            ttl_seconds=ttl_seconds,
        )

    async def refresh(self, handle: AccountOperationLeaseHandle) -> bool:
        self.refresh_calls += 1
        return True

    async def release(self, handle: AccountOperationLeaseHandle) -> bool:
        return True


class FakeRiskGuard:
    async def check_and_reserve(self, *args, **kwargs) -> RiskDecision:
        return RiskDecision(True)

    async def record_success(self, *args, **kwargs) -> None:
        return None

    async def record_failure(self, *args, **kwargs) -> None:
        return None


class CooldownRiskGuard(FakeRiskGuard):
    def __init__(self):
        self.calls = 0

    async def check_and_reserve(self, *args, **kwargs) -> RiskDecision:
        self.calls += 1
        if self.calls == 1:
            return RiskDecision(
                False,
                reason="search_cooldown",
                retry_after_seconds=17,
            )
        return RiskDecision(True)


@pytest.mark.asyncio
async def test_resource_search_merges_accounts_and_keywords_without_join_side_effects(test_db):
    accounts = [
        await _create_account(test_db, "resource-search-a"),
        await _create_account(test_db, "resource-search-b"),
    ]
    run = await _create_run(test_db, accounts)
    pool = AsyncMock()
    service = ResourceSearchService(
        test_db,
        account_pool=pool,
        group_finder=FakeGroupFinder(),
        risk_guard=FakeRiskGuard(),
        lease_manager=FakeLeaseManager(),
        keyword_delay_seconds=0,
    )

    summary = await service.run(run.id)

    assert summary["status"] == ResourceSearchStatus.COMPLETED.value
    assert summary["raw_result_count"] == 4
    assert summary["unique_result_count"] == 1
    result = (
        await test_db.execute(
            select(ResourceSearchResult).where(ResourceSearchResult.run_id == run.id)
        )
    ).scalar_one()
    assert result.telegram_group_id == 987654
    assert result.username == "global_resource_hub"
    assert result.invite_link == "https://t.me/global_resource_hub"
    assert result.discovery_count == 4
    assert json.loads(result.matched_keywords_json) == ["VPN", "机场"]
    assert json.loads(result.discovered_by_account_ids_json) == sorted(
        account.id for account in accounts
    )
    assert (
        await test_db.execute(select(func.count(Group.id)))
    ).scalar_one() == 0
    assert (
        await test_db.execute(select(func.count(AutoJoinAttempt.id)))
    ).scalar_one() == 0
    pool.sync_from_db.assert_awaited_once()


@pytest.mark.asyncio
async def test_resource_search_isolates_one_account_failure(test_db):
    accounts = [
        await _create_account(test_db, "resource-search-ok"),
        await _create_account(test_db, "resource-search-failed"),
    ]
    run = await _create_run(test_db, accounts)
    pool = AsyncMock()
    service = ResourceSearchService(
        test_db,
        account_pool=pool,
        group_finder=PartiallyFailingFinder(accounts[1].id),
        risk_guard=FakeRiskGuard(),
        lease_manager=FakeLeaseManager(),
        keyword_delay_seconds=0,
    )

    summary = await service.run(run.id)

    assert summary["status"] == ResourceSearchStatus.PARTIAL.value
    assert summary["successful_accounts"] == 1
    assert summary["failed_accounts"] == 1
    tasks = (
        (
            await test_db.execute(
                select(ResourceSearchAccountTask)
                .where(ResourceSearchAccountTask.run_id == run.id)
                .order_by(ResourceSearchAccountTask.account_id)
            )
        )
        .scalars()
        .all()
    )
    status_by_account = {task.account_id: task.status for task in tasks}
    assert status_by_account[accounts[0].id] == ResourceSearchStatus.COMPLETED.value
    assert status_by_account[accounts[1].id] == ResourceSearchStatus.FAILED.value


@pytest.mark.asyncio
async def test_create_resource_search_run_queues_only_resource_search_task(
    test_db,
    monkeypatch,
):
    account = await _create_account(test_db, "resource-search-api")
    await test_db.commit()
    queued_runs: list[tuple[int, str]] = []
    monkeypatch.setattr(
        resource_search_api,
        "enqueue_resource_search",
        lambda run_id, task_id: queued_runs.append((run_id, task_id)),
    )

    response = await resource_search_api.create_resource_search_run(
        ResourceSearchCreateRequest(
            keywords=["VPN", "vpn", "机场"],
            account_ids=[account.id, account.id],
            max_results_per_keyword=15,
        ),
        current_user={"id": 9, "role": "admin"},
        db=test_db,
    )

    run_id = response["data"]["id"]
    assert queued_runs == [(run_id, response["data"]["celery_task_id"])]
    assert response["data"]["keywords"] == ["VPN", "机场"]
    assert response["data"]["account_ids"] == [account.id]
    assert (
        await test_db.execute(select(func.count(Group.id)))
    ).scalar_one() == 0
    assert (
        await test_db.execute(select(func.count(AutoJoinAttempt.id)))
    ).scalar_one() == 0


def test_merge_resource_observations_unifies_id_and_username_only_results():
    observations = [
        ResourceObservation(
            account_id=1,
            keyword="vpn",
            group=DiscoveredGroup(
                group_id=123,
                title="Resource Hub",
                username="Same_Group",
                member_count=100,
                is_private=False,
            ),
        ),
        ResourceObservation(
            account_id=2,
            keyword="proxy",
            group=DiscoveredGroup(
                group_id=None,
                title="Resource Hub",
                username="@same_group",
                member_count=120,
                is_private=False,
            ),
        ),
    ]

    merged = merge_resource_observations(observations)

    assert len(merged) == 1
    assert merged[0].dedupe_key == "telegram:123"
    assert merged[0].discovery_count == 2
    assert merged[0].matched_keywords == {"vpn", "proxy"}


@pytest.mark.asyncio
async def test_resource_search_waits_for_risk_cooldown(test_db):
    account = await _create_account(test_db, "resource-search-cooldown")
    run = await _create_run(test_db, [account])
    waits: list[float] = []

    async def record_sleep(seconds: float) -> None:
        waits.append(seconds)

    lease_manager = FakeLeaseManager()
    service = ResourceSearchService(
        test_db,
        account_pool=AsyncMock(),
        group_finder=FakeGroupFinder(),
        risk_guard=CooldownRiskGuard(),
        lease_manager=lease_manager,
        sleep=record_sleep,
    )

    summary = await service.run(run.id)

    assert summary["status"] == ResourceSearchStatus.COMPLETED.value
    assert waits == [17]
    assert lease_manager.refresh_calls == 3


@pytest.mark.asyncio
async def test_stale_resource_search_is_requeued_without_losing_progress(test_db):
    account = await _create_account(test_db, "resource-search-stale")
    run = await _create_run(test_db, [account])
    run.status = ResourceSearchStatus.RUNNING.value
    run.celery_task_id = "old-task"
    run.heartbeat_at = datetime.utcnow() - timedelta(minutes=20)
    task = (
        await test_db.execute(
            select(ResourceSearchAccountTask).where(
                ResourceSearchAccountTask.run_id == run.id
            )
        )
    ).scalar_one()
    task.status = ResourceSearchStatus.RUNNING.value
    task.keywords_completed = 1
    task.successful_keywords = 1
    await test_db.commit()

    result = await reconcile_stale_resource_search_runs(
        test_db,
        stale_after_seconds=600,
    )

    await test_db.refresh(run)
    await test_db.refresh(task)
    assert len(result["requeued"]) == 1
    assert result["requeued"][0]["old_task_id"] == "old-task"
    assert run.status == ResourceSearchStatus.QUEUED.value
    assert run.celery_task_id != "old-task"
    assert task.status == ResourceSearchStatus.QUEUED.value
    assert task.keywords_completed == 1
    assert task.successful_keywords == 1


@pytest.mark.asyncio
async def test_incremental_results_merge_username_only_then_telegram_id(test_db):
    account = await _create_account(test_db, "resource-search-mixed")
    run = await _create_run(test_db, [account])
    service = ResourceSearchService(
        test_db,
        account_pool=AsyncMock(),
        group_finder=MixedIdentityFinder(),
        risk_guard=FakeRiskGuard(),
        lease_manager=FakeLeaseManager(),
    )

    summary = await service.run(run.id)

    results = (
        (
            await test_db.execute(
                select(ResourceSearchResult).where(ResourceSearchResult.run_id == run.id)
            )
        )
        .scalars()
        .all()
    )
    assert summary["unique_result_count"] == 1
    assert len(results) == 1
    assert results[0].dedupe_key == "telegram:321"
    assert results[0].discovery_count == 2


@pytest.mark.asyncio
async def test_running_worker_stops_when_task_id_is_superseded(test_db):
    account = await _create_account(test_db, "resource-search-superseded")
    run = await _create_run(test_db, [account])
    run.celery_task_id = "new-task"
    await test_db.commit()
    service = ResourceSearchService(
        test_db,
        account_pool=AsyncMock(),
        risk_guard=FakeRiskGuard(),
        lease_manager=FakeLeaseManager(),
    )
    service.current_task_id = "old-task"

    with pytest.raises(ResourceSearchSuperseded):
        await service._cancel_requested(run.id)


@pytest.mark.asyncio
async def test_create_run_rejects_account_already_in_active_search(test_db):
    account = await _create_account(test_db, "resource-search-conflict")
    await _create_run(test_db, [account])

    with pytest.raises(HTTPException) as exc_info:
        await resource_search_api.create_resource_search_run(
            ResourceSearchCreateRequest(
                keywords=["proxy"],
                account_ids=[account.id],
            ),
            current_user={"id": 9, "role": "admin"},
            db=test_db,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["conflicts"][0]["account_id"] == account.id


@pytest.mark.asyncio
async def test_cancel_queued_run_is_persisted_and_serialized_as_utc_z(
    test_db,
    monkeypatch,
):
    account = await _create_account(test_db, "resource-search-cancel")
    run = await _create_run(test_db, [account])
    run.celery_task_id = "cancel-task"
    await test_db.commit()
    revoked: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        celery_app.control,
        "revoke",
        lambda task_id, terminate=False: revoked.append((task_id, terminate)),
    )

    response = await resource_search_api.cancel_resource_search_run(
        run.id,
        _current_user={"id": 9, "role": "admin"},
        db=test_db,
    )

    task = (
        await test_db.execute(
            select(ResourceSearchAccountTask).where(
                ResourceSearchAccountTask.run_id == run.id
            )
        )
    ).scalar_one()
    assert response["data"]["status"] == ResourceSearchStatus.CANCELLED.value
    assert response["data"]["cancel_requested_at"].endswith("Z")
    assert response["data"]["completed_at"].endswith("Z")
    assert task.status == ResourceSearchStatus.CANCELLED.value
    assert revoked == [("cancel-task", False)]


@pytest.mark.asyncio
async def test_cancel_running_run_survives_celery_revoke_failure(
    test_db,
    monkeypatch,
):
    account = await _create_account(test_db, "resource-search-running-cancel")
    run = await _create_run(test_db, [account])
    run.status = ResourceSearchStatus.RUNNING.value
    run.celery_task_id = "running-task"
    await test_db.commit()

    def fail_revoke(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(celery_app.control, "revoke", fail_revoke)

    response = await resource_search_api.cancel_resource_search_run(
        run.id,
        _current_user={"id": 9, "role": "admin"},
        db=test_db,
    )

    await test_db.refresh(run)
    assert run.status == ResourceSearchStatus.RUNNING.value
    assert run.cancel_requested_at is not None
    assert response["data"]["cancel_requested_at"].endswith("Z")
    assert response["message"] == "已提交取消请求"


def test_resource_search_datetime_serializer_uses_utc_z():
    assert _utc_iso(datetime(2026, 8, 29, 5, 6, 7)) == "2026-08-29T05:06:07Z"


def test_all_resource_search_api_routes_require_admin():
    routes = [
        route
        for route in resource_search_api.router.routes
        if isinstance(route, APIRoute)
    ]

    assert routes
    for route in routes:
        dependency_calls = {
            dependency.call for dependency in route.dependant.dependencies
        }
        assert require_admin in dependency_calls, route.path


def test_resource_search_uses_dedicated_queue_and_worker():
    route = celery_app.conf.task_routes[
        "app.core.scheduler.tasks.resource_search_task"
    ]
    assert route["queue"] == "resource_search"

    repository_root = Path(__file__).parents[5]
    compose_text = (
        repository_root / "docker-compose.test001.yml"
    ).read_text(encoding="utf-8")
    general_worker = compose_text.split("  celery-worker:", 1)[1].split(
        "  resource-search-worker:", 1
    )[0]
    resource_worker = compose_text.split("  resource-search-worker:", 1)[1].split(
        "  celery-beat:", 1
    )[0]

    assert '"resource_search"' not in general_worker
    assert '"resource_search"' in resource_worker
    assert 'CELERY_WORKER_CONCURRENCY: "1"' in resource_worker
