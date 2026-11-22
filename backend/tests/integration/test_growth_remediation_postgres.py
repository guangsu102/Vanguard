"""Run remediation behavior against the disposable PostgreSQL fixture."""
import fakeredis.aioredis
import pytest

from tests.unit import test_growth_remediation as acceptance

pytest_plugins = ("tests.integration.test_simplified_growth_postgres",)


@pytest.mark.parametrize("scenario", [
    "unknown_send_remains_counted_after_redis_restart",
    "chat_restriction_refunds_once_and_does_not_freeze_account",
    "cross_group_failures_pause_and_enqueue_diagnosis",
    "gap_revokes_permission_but_duplicate_join_does_not",
    "left_membership_daily_target_retires_without_telegram",
    "runtime_exposes_effective_limits_and_durable_usage",
    "observation_limit_survives_recovery_expiry_without_changing_business_config",
    "late_result_cannot_replace_newer_daily_target",
])
async def test_remediation_with_real_constraints(pg, monkeypatch, scenario):
    db, _assert_no_transaction = pg
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await getattr(acceptance, 'test_' + scenario)(db, monkeypatch, redis)
