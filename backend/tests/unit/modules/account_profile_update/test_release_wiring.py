from pathlib import Path

BACKEND = Path(__file__).resolve().parents[4]


def test_account_profile_update_release_wiring_is_complete():
    celery = (BACKEND / "app" / "celery.py").read_text(encoding="utf-8")
    worker = (BACKEND / "app" / "core" / "scheduler" / "worker.py").read_text(
        encoding="utf-8"
    )
    main = (BACKEND / "app" / "main.py").read_text(encoding="utf-8")
    migrations = (BACKEND / "scripts" / "apply_sql_migrations.py").read_text(
        encoding="utf-8"
    )
    api = (BACKEND / "app" / "api" / "account_profile_updates.py").read_text(
        encoding="utf-8"
    )

    assert '"app.modules.account_profile_update.tasks"' in celery
    assert '"account_profile_update": 1' in celery
    assert '"account-profile-update-worker-every-10s"' in celery
    assert '"queue": "account_profile_update"' in celery
    assert '"account_profile_update": {"concurrency": 1' in worker
    assert '"worker-account-profile-update"' in worker
    assert "account_profile_updates" in main
    assert (BACKEND / "migrations" / "055_add_account_profile_updates.sql").is_file()
    assert "--files" in migrations
    assert '"055_add_account_profile_updates.sql"' in migrations
    assert "Depends(require_admin)" in api
    assert "Idempotency-Key" in api
