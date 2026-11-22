"""Run only against an explicitly named disposable PostgreSQL test database."""
import os
from datetime import datetime
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import fakeredis.aioredis
import pytest
import pytest_asyncio
from sqlalchemy import create_mock_engine, event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import Base
from app.core.group.models import GroupAccountMembership as Member
from app.modules.acquisition.models import AdDeliveryLog, AutoJoinAttempt
from tests.conftest import _import_models
from tests.unit.test_simplified_growth import setup


@pytest_asyncio.fixture
async def pg():
    url = os.getenv("VANGUARD_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("VANGUARD_TEST_POSTGRES_URL is required; never use the application database")
    assert make_url(url).database == "vanguard_growth_test"
    schema = "growth_test_" + uuid4().hex
    observer = create_async_engine(url, pool_size=1, max_overflow=0)
    async with observer.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, pool_size=2, max_overflow=1,
        connect_args={"server_settings": {"search_path": schema, "application_name": schema}})
    _import_models()
    try:
        required = {"telegram_account", "telegram_account_operation_config", "group", "group_account_membership",
                    "ad_campaign", "ad_creative", "account_ad_binding", "ad_delivery_log",
                    "acquisition_auto_join_attempt", "group_ad_frequency", "group_ad_frequency_event",
                    "group_ad_profile", "system_setting", "owned_group_assets", "account_risk_event",
                    "account_outbound_attempt", "xboard_callback", "group_qualification_audit",
                    "account_environment_event"}
        pending = list(required)
        while pending:
            table = Base.metadata.tables[pending.pop()]
            for foreign_key in table.foreign_keys:
                parent = foreign_key.column.table.name
                if parent not in required:
                    required.add(parent)
                    pending.append(parent)
        ddl = []
        required.add("telegram_event_inbox")
        mock = create_mock_engine("postgresql://", lambda statement, *_a, **_k: ddl.append(
            str(statement.compile(dialect=mock.dialect))))
        Base.metadata.create_all(mock, tables=[t for t in Base.metadata.sorted_tables if t.name in required], checkfirst=False)
        async with engine.begin() as conn:
            driver = await conn.get_raw_connection()
            await driver.driver_connection.execute(";\n".join(ddl))
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            async def assert_no_transaction():
                assert not db.in_transaction()
                async with observer.connect() as conn:
                    count = await conn.scalar(text("SELECT count(*) FROM pg_stat_activity "
                        "WHERE application_name=:name AND xact_start IS NOT NULL"), {"name": schema})
                    assert count == 0
            yield db, assert_no_transaction
    finally:
        await engine.dispose()
        async with observer.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await observer.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["shared_group_cooldown", "bound_creatives"])
async def test_due_candidate_selection_on_postgres(pg, monkeypatch, scenario):
    from tests.unit.test_simplified_growth import (
        test_shared_group_cooldown_does_not_starve_other_due_groups,
        test_weighted_choice_receives_all_enabled_bound_text_creatives,
    )
    db, _assert_no_transaction = pg
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    check = (test_shared_group_cooldown_does_not_starve_other_due_groups
             if scenario == "shared_group_cooldown" else
             test_weighted_choice_receives_all_enabled_bound_text_creatives)
    await check(db, monkeypatch, redis)


@pytest.mark.asyncio
async def test_verified_recovery_locks_only_configuration_row(pg, monkeypatch, tmp_path):
    from app.core.account.models import AccountOperationConfig, AccountStatus, TelegramAccount
    from app.core.account.recovery import AccountRecoveryService, RecoveryResult
    from tests.unit.test_account_error_recovery import recovery_account

    db, assert_no_transaction = pg
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("app.core.redis.redis_client", redis)
    monkeypatch.setattr("app.core.account.session_quarantine._root", lambda: tmp_path)
    # This isolated database can be reached through an intercontinental SSH
    # tunnel. Exercise the real locks without using the production LAN timeout.
    monkeypatch.setattr("app.core.account.session_quarantine.DB_TIMEOUT_SECONDS", 30)
    await recovery_account(db)
    db.add(AccountOperationConfig(account_id=35, enabled=True))
    await db.commit()

    async def verify(*_args, **_kwargs):
        await assert_no_transaction()
        return RecoveryResult(True, "identity_verified")

    pool = NS(add_account_from_db=AsyncMock(), verify_error_account=verify, remove_account=AsyncMock())
    outcome = await AccountRecoveryService(db, pool).recover(35)
    assert outcome.ok
    assert (await db.get(TelegramAccount, 35)).status == AccountStatus.ONLINE
    pool.remove_account.assert_awaited_once()


@pytest.mark.asyncio
async def test_ad_wait_has_no_postgres_transaction_or_intent(pg, monkeypatch):
    db, assert_no_transaction = pg
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    svc, account, member, campaign, creative, client = await setup(db, monkeypatch, redis)
    writes = []
    commits = []
    def before(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)
    event.listen(db.bind.sync_engine, "before_cursor_execute", before)
    event.listen(db.sync_session, "after_commit", lambda _s: commits.append(1))
    async def send(*_args, **_kwargs):
        await assert_no_transaction()
        assert any("ad_delivery_log" in sql.lower() for sql in writes)
        commits.clear()
        return NS(id=456)
    client.send_message.side_effect = send
    outcome = await svc.send(account.id, member.id, campaign.id, creative.id, -1000000000123)
    assert outcome["message_id"] == 456
    assert len(commits) == 1
    assert all("account_outbound_attempt" not in sql.lower() for sql in writes)
    assert await db.scalar(select(AdDeliveryLog.telegram_message_id)) == 456


@pytest.mark.asyncio
@pytest.mark.parametrize("paused", [False, True])
async def test_cutover_expansion_conversion_and_retry_are_idempotent(pg, monkeypatch, paused):
    from contextlib import asynccontextmanager
    from pathlib import Path

    from scripts import activate_simple_growth as activation

    db, _assert_no_transaction = pg
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    _svc, account, member, campaign, creative, _client = await setup(db, monkeypatch, redis)
    member.review_status, member.ad_status = "manual_required", "paused" if paused else "blocked"
    db.add(AdDeliveryLog(account_id=account.id, group_id=member.group_id,
        telegram_group_id=member.telegram_group_id,
        ad_campaign_id=campaign.id, creative_id=creative.id, status="unknown"))
    await db.commit()
    # The additive DDL must be safe to run again before conversion.
    migration = (Path(__file__).parents[2] / "migrations" / "077_growth_permissions.sql").read_text()
    async with db.bind.begin() as conn:
        driver = await conn.get_raw_connection()
        await driver.driver_connection.execute(migration)
        await driver.driver_connection.execute(migration)
    @asynccontextmanager
    async def session():
        yield db
    monkeypatch.setattr(activation, "get_db_session", session)
    monkeypatch.setattr(activation, "get_redis", AsyncMock(return_value=redis))
    await activation.activate(True)
    await db.refresh(member)
    assert member.review_status == ("rejected" if paused else "pending") and member.can_send_text is None
    assert member.ad_status == ("paused" if paused else "blocked")
    assert member.next_ad_allowed_at > datetime.utcnow()
    due = member.next_ad_allowed_at
    await db.commit()
    await activation.activate(True)
    await db.refresh(member)
    assert member.next_ad_allowed_at == due
    assert await redis.get("vanguard:growth:v2:group:-1000000000123:due") is not None


@pytest.mark.asyncio
async def test_paused_departure_survives_schema_migration(pg, monkeypatch):
    from pathlib import Path

    from app.modules.acquisition.simple_runtime import apply_fact
    from scripts.apply_sql_migrations import _split_sql_statements
    db, _assert_no_transaction = pg
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    _svc, account, member, *_ = await setup(db, monkeypatch, redis)
    account_id, member_id, target = account.id, member.id, member.telegram_group_id
    for statement in _split_sql_statements(Path("migrations/077_growth_permissions.sql").read_text()):
        await db.execute(text(statement))
    await db.commit()
    member.ad_status = "paused"
    await db.commit()
    from datetime import UTC
    await apply_fact(db, account_id, {"kind": "membership", "peer": target,
        "at": datetime.now(UTC).timestamp(), "member": False})
    await db.commit()
    await db.refresh(member)
    assert member.id == member_id and member.status == "left" and member.ad_status == "paused"


@pytest.mark.asyncio
async def test_owner_forward_wait_releases_dispatcher_transaction(pg, monkeypatch):
    from app.core.scheduler.growth_dispatch import execute
    db, assert_no_transaction = pg
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    _svc, account, *_ = await setup(db, monkeypatch, redis)
    monkeypatch.setattr("app.core.account.session_exec.owner_id", AsyncMock(return_value="other-owner"))
    async def forward(*_args):
        await assert_no_transaction()
        return {"result": {"processed": 1}, "interval": 60}
    monkeypatch.setattr("app.core.account.session_exec.forward", forward)
    outcome, interval = await execute(db, "ads", account.id)
    assert outcome["processed"] == 1 and interval == 60


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_id", [False, True])
async def test_join_wait_has_no_postgres_transaction_or_intent(pg, monkeypatch, legacy_id):
    from telethon.tl.types import Channel, ChatPhotoEmpty

    from app.core.group.models import Group
    from app.modules.acquisition.join_budget import JoinRequestBudgetService
    db, assert_no_transaction = pg
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    svc, account, member, _campaign, _creative, _client = await setup(db, monkeypatch, redis)
    group = await db.get(Group, member.group_id)
    await db.delete(member)
    group.status = "pending_join"
    if legacy_id:
        group.group_id = 123
    await db.commit()
    monkeypatch.setattr(JoinRequestBudgetService, "eligibility_reason", AsyncMock(return_value=None))
    svc.service._search_filter_settings = AsyncMock(return_value=NS(cjk_title_required=False))
    svc.service._title_blacklist_match = lambda *_args: False
    entered = False
    writes = []
    commits = []
    def before(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)
    event.listen(db.bind.sync_engine, "before_cursor_execute", before)
    event.listen(db.sync_session, "after_commit", lambda _s: commits.append(1))
    class Client:
        async def get_entity(self, _target):
            await assert_no_transaction()
            return Channel(id=123, title="测试", photo=ChatPhotoEmpty(), date=datetime.utcnow(), megagroup=True)
        async def __call__(self, _request):
            nonlocal entered
            await assert_no_transaction()
            assert not writes
            assert await redis.get("vanguard:growth:v2:lock:join-target:-1000000000123")
            commits.clear()
            entered = True
            return NS()
    svc.pool.acquire_by_id.return_value.client = Client()
    outcome = await svc.join(account.id)
    assert entered and outcome["joined"] == 1
    assert len(commits) == 1
    assert await db.scalar(select(Member.status)) == "joined"
    assert await db.scalar(select(Member.telegram_group_id)) == -1000000000123
    assert await db.scalar(select(AutoJoinAttempt.status)) == "success"


@pytest.mark.parametrize("scenario", ["three_group_failures_trigger_one_exit_and_no_review",
    "success_clears_streak_and_never_creates_survival_work", "unknown_next_slot_can_send_new_operation",
    "pre_rpc_record_and_interrupted_recovery_never_replays", "exit_stops_after_three_failures",
    "cutover_preview_apply_and_repeat"])
async def test_send_result_contract_on_postgres(pg, monkeypatch, tmp_path, scenario):
    from tests.unit import test_send_result_policy as cases
    db, _observer = pg
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    kwargs = dict(test_db=db, monkeypatch=monkeypatch, redis=redis)
    if scenario == "cutover_preview_apply_and_repeat":
        kwargs["tmp_path"] = tmp_path
    await getattr(cases, "test_" + scenario)(**kwargs)


@pytest.mark.parametrize("scenario", ["verification", "account", "creative", "binding", "protection",
    "paused_account_interrupted_attempt_recovers_without_journal", "live_owner_excludes_global_interrupted_recovery",
    "bounded_recovery_preserves_receipts_beyond_current_page",
    "observation_preserves_unexpired_recovery_limit", "observation_rejects_join_quota_violation",
    "failed_observation_can_be_explicitly_restarted", "observation_start_is_idempotent_and_restart_preserves_manual_pause",
    "live_age_metrics_exclude_terminal_history"])
async def test_send_result_omissions_on_postgres(pg, monkeypatch, tmp_path, scenario):
    from tests.unit import test_send_result_omissions as cases
    db, _observer = pg
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    kwargs = dict(test_db=db, monkeypatch=monkeypatch, redis=redis)
    if scenario in {"verification", "account", "creative", "binding", "protection"}:
        await cases.test_revocation_during_admission_prevents_rpc(**kwargs, revoked=scenario)
    else:
        if scenario in {"failed_observation_can_be_explicitly_restarted", "observation_start_is_idempotent_and_restart_preserves_manual_pause"}:
            kwargs["tmp_path"] = tmp_path
        await getattr(cases, "test_" + scenario)(**kwargs)


@pytest.mark.parametrize("changed_while_paused", [False, True])
async def test_release_resume_locks_only_config_and_preserves_new_pause(pg, monkeypatch, changed_while_paused):
    from contextlib import asynccontextmanager
    import json
    from pathlib import Path
    import runpy
    from app.core import database
    from app.core.account.models import AccountOperationConfig
    from app.core.settings_models import SystemSetting
    db, _observer = pg
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    _svc, account, *_rest = await setup(db, monkeypatch, redis)
    config = await db.scalar(select(AccountOperationConfig).where(AccountOperationConfig.account_id == account.id))
    config.enabled = False
    await db.flush()
    started = datetime.utcnow().isoformat()
    state = {"operations": [{"id": config.id, "enabled": True, "paused_updated_at": config.updated_at.isoformat()}],
             "observation_before": {"send_result_started_at": started}}
    db.add_all([SystemSetting(key="growth.send_result.release_pause.r2", value=json.dumps(state)),
                SystemSetting(key="growth.canary.account.2", value=json.dumps(state["observation_before"]))])
    await db.commit()
    if changed_while_paused:
        config.message_interval_seconds += 1
        await db.commit()
    @asynccontextmanager
    async def session():
        yield db
    monkeypatch.setattr(database, "get_db_session", session)
    path = Path(os.getenv("VANGUARD_TEST_RELEASE_SCRIPT", str(Path(__file__).parents[3] / "scripts/ops/send_result_omissions_release.py")))
    operator = runpy.run_path(str(path))
    namespace = {}
    exec(compile(operator["RESUME"].replace("asyncio.run(work())", ""), str(path), "exec"), namespace)
    await namespace["work"]()
    assert config.enabled is not changed_while_paused
    marker = json.loads((await db.get(SystemSetting, "growth.send_result.release_pause.r2")).value)
    assert marker["resumed_at"]
    assert json.loads((await db.get(SystemSetting, "growth.canary.account.2")).value)["send_result_started_at"] == started
