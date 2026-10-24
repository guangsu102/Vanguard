"""P5 release-candidate preflight and freeze CLI.

Run from the backend root before every P5 deployment stage::

    python scripts/p5_preflight.py --check \
        --base-url https://vanguard.pipenai.xyz \
        --metrics-token-file /run/secrets/metrics.token
    python scripts/p5_preflight.py --freeze \
        --release-id p5-20261024-a1b2c3 --image-digest sha256:...

The preflight verifies the release-candidate contract: canonical SQL
migrations applied (forward-only), database/Redis readiness, Actor fencing
state (no expired leases, no active FloodWait pause), no unexplained
reconciliation aging, no aged ad ``pending/unknown`` rows, protected
``/health`` / ``/metrics`` / ``/api/operations/status`` access, history
governance still dry-run, and a read-only canary preview.  ``--freeze``
persists one immutable release record with the pinned image digest and
migration revision; a fail-closed preflight never freezes.

Exit code is 0 when every check passes and 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select, text  # noqa: E402

from scripts.apply_sql_migrations import DEFAULT_MIGRATIONS  # noqa: E402


def alembic_head() -> str | None:
    """The current Alembic head revision, read statically (no DB needed)."""
    try:
        from alembic.config import Config as AlembicConfig
        from alembic.script import ScriptDirectory

        root = Path(__file__).resolve().parents[1]
        config = AlembicConfig(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        return ScriptDirectory.from_config(config).get_current_head()
    except Exception:
        return None


def _check(name: str, ok: bool, detail: str = "", *, critical: bool = True) -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail, "critical": critical}


async def check_migrations(db) -> list[dict]:
    """Canonical SQL migrations applied; deployments are forward-only."""
    rows = (
        await db.execute(text("SELECT filename FROM schema_migration_history"))
    ).all()
    applied = {row[0] for row in rows}
    missing = [name for name in DEFAULT_MIGRATIONS if name not in applied]
    revision = alembic_head()
    checks = [
        _check(
            "canonical_migrations_applied",
            not missing,
            f"missing={missing}" if missing else f"applied={len(applied)}",
        ),
        _check(
            "alembic_head_revision",
            revision is not None,
            revision or "alembic script directory unavailable",
            critical=False,
        ),
    ]
    return checks


async def check_dependencies(db, cache) -> list[dict]:
    from app.core.runtime_health import dependencies

    try:
        await dependencies()
        return [_check("dependencies_ready", True, "database+redis ready")]
    except Exception as exc:
        return [_check("dependencies_ready", False, str(exc))]


async def check_release_safety(db, now: datetime) -> list[dict]:
    """Fencing state, reconciliation aging and history-governance mode."""
    from app.core.account.control_models import ReconciliationTask
    from app.core.account.models import TelegramAccount
    from app.core.config import settings
    from app.core.metrics import SLO
    from app.modules.acquisition.models import AdDeliveryLog
    from app.modules.acquisition.p2_models import GrowthOutbox
    from app.modules.acquisition.p5_rollout import _reconciliation_health

    checks: list[dict] = []

    expired_outbox = await db.scalar(
        select(func.count()).select_from(GrowthOutbox).where(
            GrowthOutbox.state == "leased",
            GrowthOutbox.lease_expires_at <= now,
        )
    )
    checks.append(_check("no_expired_outbox_leases", int(expired_outbox or 0) == 0,
                         f"count={int(expired_outbox or 0)}"))
    expired_tasks = await db.scalar(
        select(func.count()).select_from(ReconciliationTask).where(
            ReconciliationTask.state == "leased",
            ReconciliationTask.lease_expires_at <= now,
        )
    )
    checks.append(_check("no_expired_reconciliation_leases", int(expired_tasks or 0) == 0,
                         f"count={int(expired_tasks or 0)}"))
    floodwait = await db.scalar(
        select(func.count()).select_from(TelegramAccount).where(
            TelegramAccount.risk_reason == "telegram_read_flood_wait",
            TelegramAccount.risk_pause_until > now,
        )
    )
    checks.append(_check("no_active_floodwait_pause", int(floodwait or 0) == 0,
                         f"accounts={int(floodwait or 0)}"))
    reconciliation = await _reconciliation_health(db, now)
    checks.append(_check(
        "no_unexplained_reconciliation_aging",
        int(reconciliation.get("reconciliation_unexplained") or 0) == 0,
        f"oldest_age={reconciliation.get('reconciliation_oldest_age_seconds'):.0f}s "
        f"unexplained={reconciliation.get('reconciliation_unexplained')}",
    ))
    aged_cutoff = now - timedelta(seconds=int(SLO["ad_unknown_warning_seconds"]))
    aged_ads = await db.scalar(
        select(func.count()).select_from(AdDeliveryLog).where(
            AdDeliveryLog.status.in_(["pending", "unknown", "sending", "reconciliation_required"]),
            AdDeliveryLog.created_at < aged_cutoff,
        )
    )
    checks.append(_check("no_aged_ad_pending_unknown", int(aged_ads or 0) == 0,
                         f"count={int(aged_ads or 0)}"))
    dry_run = (
        not settings.P4_HISTORY_GOVERNANCE
        or settings.P4_HISTORY_GOVERNANCE_DRY_RUN
        or not settings.P5_HISTORY_APPLY
    )
    checks.append(_check("history_governance_dry_run", dry_run,
                         f"P4_HISTORY_GOVERNANCE={settings.P4_HISTORY_GOVERNANCE} "
                         f"dry_run={settings.P4_HISTORY_GOVERNANCE_DRY_RUN} "
                         f"P5_HISTORY_APPLY={settings.P5_HISTORY_APPLY}"))
    return checks


async def check_endpoints(base_url: str, metrics_token: str | None) -> list[dict]:
    """Protected /health, /metrics (bearer) and /api/operations/status access."""
    import httpx

    checks: list[dict] = []
    async with httpx.AsyncClient(timeout=10, verify=False) as client:
        try:
            response = await client.get(base_url.rstrip("/") + "/health")
            checks.append(_check("health_endpoint_200", response.status_code == 200,
                                 f"status={response.status_code}"))
        except Exception as exc:
            checks.append(_check("health_endpoint_200", False, str(exc)))
        try:
            headers = {"Authorization": f"Bearer {metrics_token}"} if metrics_token else {}
            response = await client.get(base_url.rstrip("/") + "/metrics", headers=headers)
            body_ok = "vanguard_" in response.text
            checks.append(_check(
                "metrics_endpoint_readable",
                response.status_code == 200 and body_ok,
                f"status={response.status_code} vanguard_metrics={body_ok}",
            ))
        except Exception as exc:
            checks.append(_check("metrics_endpoint_readable", False, str(exc)))
        try:
            response = await client.get(base_url.rstrip("/") + "/metrics")
            protected = response.status_code in (401, 403, 404)
            checks.append(_check("metrics_endpoint_protected_without_token", protected,
                                f"status={response.status_code}"))
        except Exception as exc:
            checks.append(_check("metrics_endpoint_protected_without_token", False, str(exc)))
        try:
            response = await client.get(base_url.rstrip("/") + "/api/operations/status")
            protected = response.status_code in (401, 403)
            checks.append(_check("operations_status_requires_auth", protected,
                                f"status={response.status_code}"))
        except Exception as exc:
            checks.append(_check("operations_status_requires_auth", False, str(exc)))
    return checks


async def preview_canary(db, now: datetime) -> dict:
    """Read-only preview of the automatic canary account selection."""
    from app.modules.acquisition.p5_rollout import select_canary_account

    pick = await select_canary_account(db, now)
    return {
        "selected_account_id": pick.account_id,
        "reason": pick.reason,
        "healthy_candidates": len(pick.report.get("ordered_healthy") or []),
        "excluded_count": pick.report.get("excluded_count"),
    }


async def run(args: argparse.Namespace) -> int:
    from app.core import database as db_module
    from app.core.config import settings
    from app.core.redis import close_redis, get_redis, init_redis

    await db_module.init_db(create_tables=False)
    await init_redis()
    cache = await get_redis()
    now = datetime.utcnow()
    checks: list[dict] = []
    async with db_module.get_db_session() as db:
        checks += await check_migrations(db)
        checks += await check_dependencies(db, cache)
        checks += await check_release_safety(db, now)
        metrics_token = None
        if args.metrics_token:
            metrics_token = args.metrics_token
        elif args.metrics_token_file:
            metrics_token = Path(args.metrics_token_file).read_text(encoding="utf-8").strip()
        checks += await check_endpoints(args.base_url, metrics_token)
        canary = await preview_canary(db, now)

        revision = alembic_head() or "unknown"
        image_digest = args.image_digest or "unpinned"
        critical_failures = [c for c in checks if c["critical"] and not c["ok"]]
        report = {
            "checked_at": now.isoformat(),
            "image_digest": image_digest,
            "migration_revision": revision,
            "p5": {
                "mode": settings.p5_rollout_mode,
                "release_id": settings.P5_RELEASE_ID,
                "cohort_percent": settings.P5_COHORT_PERCENT,
                "canary_account_ids": sorted(settings.p5_canary_accounts),
                "history_apply": settings.P5_HISTORY_APPLY,
            },
            "canary_preview": canary,
            "checks": checks,
            "preflight_passed": not critical_failures,
        }
        if args.freeze:
            if critical_failures:
                report["freeze"] = {
                    "ok": False,
                    "error": "preflight_failed_refusing_freeze",
                    "failed": [c["name"] for c in critical_failures],
                }
            else:
                from app.modules.acquisition.p5_rollout import freeze_release_candidate

                if not args.release_id:
                    report["freeze"] = {"ok": False, "error": "release_id_required"}
                else:
                    try:
                        record = await freeze_release_candidate(
                            db,
                            release_id=args.release_id,
                            image_digest=image_digest,
                            migration_revision=revision,
                        )
                        report["freeze"] = {
                            "ok": True,
                            "release_id": record.release_id,
                            "status": record.status,
                        }
                    except ValueError as exc:
                        report["freeze"] = {"ok": False, "error": str(exc)}
    print(json.dumps(report, indent=2, default=str))
    await close_redis()
    await db_module.close_db()
    return 0 if not critical_failures else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="P5 release-candidate preflight and freeze.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Run the preflight checks only")
    mode.add_argument("--freeze", action="store_true",
                      help="Freeze an immutable release record (requires a passing preflight)")
    parser.add_argument("--release-id", default="", help="Immutable release id for --freeze")
    parser.add_argument("--image-digest", default="", help="Pinned image digest for --freeze")
    parser.add_argument("--base-url", default="http://127.0.0.1:18080",
                        help="Base URL for endpoint checks (backend loopback or public origin)")
    parser.add_argument("--metrics-token", default=None, help="Production metrics bearer token")
    parser.add_argument("--metrics-token-file", default=None,
                        help="File containing the production metrics bearer token")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
