from __future__ import annotations

import importlib.util
import re
import sys
import tarfile
from pathlib import Path, PurePosixPath
from types import ModuleType
from unittest.mock import Mock

from scripts.apply_sql_migrations import DEFAULT_MIGRATIONS

REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
DEPLOY_SCRIPT = REPOSITORY_ROOT / "scripts" / "codex_deploy_automation.py"
SAFETY_DEFAULTS = (
    "P0_SAFETY_GATE_ENABLED: ${P0_SAFETY_GATE_ENABLED:-true}",
    "P0_SAFETY_GATE_FAIL_CLOSED: ${P0_SAFETY_GATE_FAIL_CLOSED:-true}",
    "OWNED_GROUP_MODULE_ENABLED: ${OWNED_GROUP_MODULE_ENABLED:-true}",
    "OWNED_GROUP_EXECUTION_ENABLED: ${OWNED_GROUP_EXECUTION_ENABLED:-false}",
    "OWNED_GROUP_KILL_SWITCH_ENABLED: ${OWNED_GROUP_KILL_SWITCH_ENABLED:-false}",
    "OWNED_GROUP_ROLLOUT_MAX_ACCOUNTS: ${OWNED_GROUP_ROLLOUT_MAX_ACCOUNTS:-2000}",
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
    assert migration in DEFAULT_MIGRATIONS
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


def test_production_deploy_scripts_load_the_same_env_for_compose_interpolation() -> None:
    command = (
        "docker compose --env-file .env.production "
        "-f docker-compose.production.yml"
    )
    for relative_path in (
        "scripts/codex_deploy_automation.py",
        "scripts/codex_deploy_group_pool.py",
        "scripts/codex_fix_redis_production.py",
    ):
        source = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")
        assert command in source
        assert "docker compose -f docker-compose.production.yml" not in source


def test_multi_worker_registers_owned_group_queue() -> None:
    worker = (
        REPOSITORY_ROOT / "backend" / "app" / "core" / "scheduler" / "worker.py"
    ).read_text(encoding="utf-8")
    assert '"owned_group": {' in worker
    assert '("worker-owned-group", ["owned_group"]' in worker


def _load_deploy_module(monkeypatch):
    monkeypatch.setitem(sys.modules, "paramiko", ModuleType("paramiko"))
    monkeypatch.setitem(sys.modules, "socks", ModuleType("socks"))
    spec = importlib.util.spec_from_file_location("codex_deploy_automation_test", DEPLOY_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_full_deploy(monkeypatch, tmp_path: Path, *argv: str):
    module = _load_deploy_module(monkeypatch)
    archive = tmp_path / "release.tar.gz"
    archive.write_bytes(b"release")
    client = Mock()
    commands: list[str] = []

    monkeypatch.setattr(sys, "argv", ["codex_deploy_automation.py", *argv])
    monkeypatch.setattr(module, "build_archive", lambda: (archive, 1))
    monkeypatch.setattr(module, "connect", lambda: client)
    monkeypatch.setattr(
        module,
        "upload_archive",
        lambda active_client, _archive, _timestamp: (
            active_client,
            "/tmp/release.tar.gz",
        ),
    )
    monkeypatch.setattr(
        module,
        "run",
        lambda _client, command, **_kwargs: commands.append(command) or "",
    )
    monkeypatch.setattr(
        module,
        "wait_for_health",
        lambda _client: commands.append("__HEALTH__"),
    )
    monkeypatch.setattr(module, "env_check_command", lambda: "__ENV_CHECK__")
    monkeypatch.setattr(
        module,
        "apply_migrations_compose_command",
        lambda: "__COMPOSE_MIGRATION__",
    )

    assert module.main() == 0
    return module, commands


def _full_stack_start(commands: list[str], module) -> str:
    suffix = "up -d --force-recreate " + " ".join(module.MAINLINE_SERVICES)
    matches = [command for command in commands if command.endswith(suffix)]
    assert len(matches) == 1
    return matches[0]


def test_full_archive_excludes_backend_virtualenv(tmp_path, monkeypatch) -> None:
    module = _load_deploy_module(monkeypatch)
    app_file = tmp_path / "backend" / "app" / "main.py"
    venv_file = tmp_path / "backend" / ".venv" / "Scripts" / "python.exe"
    app_file.parent.mkdir(parents=True)
    venv_file.parent.mkdir(parents=True)
    app_file.write_text("app = object()\n", encoding="utf-8")
    venv_file.write_bytes(b"local-only")

    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "ARCHIVE_PATH", tmp_path / "release.tar.gz")
    monkeypatch.setattr(module, "INCLUDE_DIRS", ["backend"])

    archive_path, _file_count = module.build_archive()
    with tarfile.open(archive_path, "r:gz") as archive:
        names = archive.getnames()

    assert "backend/app/main.py" in names
    assert all(".venv" not in PurePosixPath(name).parts for name in names)


def test_existing_database_migrates_before_long_lived_backend(
    tmp_path,
    monkeypatch,
) -> None:
    module, commands = _run_full_deploy(monkeypatch, tmp_path)
    db_start = next(command for command in commands if command.endswith("up -d postgres redis"))
    full_start = _full_stack_start(commands, module)

    assert commands.count("__COMPOSE_MIGRATION__") == 1
    assert not hasattr(module, "apply_migrations_command")
    assert commands.index(db_start) < commands.index("__COMPOSE_MIGRATION__")
    assert commands.index("__COMPOSE_MIGRATION__") < commands.index(full_start)
    assert all(
        "docker exec -i vanguard-backend env PYTHONPATH=/app "
        "python /app/scripts/apply_sql_migrations.py" not in command
        for command in commands
    )


def test_bootstrap_keeps_init_order_without_extra_migration(
    tmp_path,
    monkeypatch,
) -> None:
    module, commands = _run_full_deploy(monkeypatch, tmp_path, "--bootstrap")
    db_start = next(command for command in commands if command.endswith("up -d postgres redis"))
    db_init = next(
        command
        for command in commands
        if "--profile bootstrap run --rm db-init" in command
    )
    admin_init = next(
        command
        for command in commands
        if "--profile bootstrap run --rm --no-deps admin-init" in command
    )
    full_start = _full_stack_start(commands, module)

    assert "__COMPOSE_MIGRATION__" not in commands
    assert commands.index(db_start) < commands.index(db_init)
    assert commands.index(db_init) < commands.index(admin_init)
    assert commands.index(admin_init) < commands.index(full_start)


def test_deploy_ssh_rejects_unknown_host_keys() -> None:
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    connect_source = deploy[
        deploy.index("def connect()") : deploy.index("def assert_oracle4c24g_target()")
    ]

    assert "client.load_system_host_keys()" in connect_source
    assert "paramiko.RejectPolicy()" in connect_source
    assert "AutoAddPolicy" not in connect_source
