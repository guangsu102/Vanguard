from __future__ import annotations

import re
from pathlib import Path

from scripts.apply_sql_migrations import DEFAULT_MIGRATIONS

REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
SAFETY_DEFAULTS = (
    "P0_SAFETY_GATE_ENABLED: ${P0_SAFETY_GATE_ENABLED:-true}",
    "P0_SAFETY_GATE_FAIL_CLOSED: ${P0_SAFETY_GATE_FAIL_CLOSED:-true}",
    "OWNED_GROUP_MODULE_ENABLED: ${OWNED_GROUP_MODULE_ENABLED:-true}",
    "OWNED_GROUP_EXECUTION_ENABLED: ${OWNED_GROUP_EXECUTION_ENABLED:-false}",
    "OWNED_GROUP_KILL_SWITCH_ENABLED: ${OWNED_GROUP_KILL_SWITCH_ENABLED:-false}",
    "OWNED_GROUP_ROLLOUT_MAX_ACCOUNTS: ${OWNED_GROUP_ROLLOUT_MAX_ACCOUNTS:-2}",
)


def _service_block(compose_text: str, service: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(service)}:\s*\n(.*?)(?=^  [A-Za-z0-9_-]+:\s*\n|\Z)",
        compose_text,
    )
    assert match is not None, f"missing compose service: {service}"
    return match.group(1)


def test_owned_group_sql_is_in_curated_default_migration_chain() -> None:
    migration = "044_add_owned_group_orchestration.sql"
    assert migration in DEFAULT_MIGRATIONS
    sql = (REPOSITORY_ROOT / "backend" / "migrations" / migration).read_text(
        encoding="utf-8"
    )
    assert "uq_owned_group_invite_links_one_active" in sql
    assert "WHERE is_active = TRUE" in sql


def test_owned_group_governance_sql_is_in_curated_default_migration_chain() -> None:
    migration = "047_add_owned_group_governance.sql"
    assert migration in DEFAULT_MIGRATIONS
    sql = (REPOSITORY_ROOT / "backend" / "migrations" / migration).read_text(
        encoding="utf-8"
    )
    assert "governance_status" in sql
    assert "idx_owned_group_assets_guardian_status" in sql


def test_owned_group_member_observation_sql_is_last_in_default_migration_chain() -> None:
    migration = "052_add_owned_group_member_observations.sql"
    assert DEFAULT_MIGRATIONS[-1] == migration
    sql = (REPOSITORY_ROOT / "backend" / "migrations" / migration).read_text(
        encoding="utf-8"
    )
    assert "CREATE TABLE IF NOT EXISTS owned_group_member_observations" in sql
    assert "uq_owned_group_member_observations_asset_user" in sql


def test_deploy_automation_uses_curated_runner_not_removed_raw_migrations() -> None:
    deploy = (REPOSITORY_ROOT / "scripts" / "codex_deploy_automation.py").read_text(
        encoding="utf-8"
    )
    assert "apply_migrations_compose_command()" in deploy
    assert "019_group_search_keyword_normalized.sql" not in deploy
    assert "020_keyword_trigger_review.sql" not in deploy


def test_test001_and_production_wire_safe_owned_group_runtime_defaults() -> None:
    for compose_name in ("docker-compose.test001.yml", "docker-compose.production.yml"):
        compose = (REPOSITORY_ROOT / compose_name).read_text(encoding="utf-8")
        for service in ("backend", "celery-worker", "celery-beat"):
            block = _service_block(compose, service)
            for setting in SAFETY_DEFAULTS:
                assert setting in block, f"{compose_name}:{service} missing {setting}"
        worker = _service_block(compose, "celery-worker")
        if compose_name == "docker-compose.test001.yml":
            owned_worker = _service_block(compose, "owned-group-worker")
            assert 'CELERY_WORKER_CONCURRENCY: "1"' in owned_worker
            assert '"owned_group"' in owned_worker
            assert '"owned_group"' not in worker
            for setting in SAFETY_DEFAULTS:
                assert setting in owned_worker
        else:
            assert '"multi"' in worker


def test_multi_worker_registers_owned_group_queue() -> None:
    worker = (
        REPOSITORY_ROOT / "backend" / "app" / "core" / "scheduler" / "worker.py"
    ).read_text(encoding="utf-8")
    assert '"owned_group": {' in worker
    assert '("worker-owned-group", ["owned_group"]' in worker
