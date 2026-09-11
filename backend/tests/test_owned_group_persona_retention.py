from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.modules.owned_group.messaging_models import GroupAccountMessageExecution
from app.modules.owned_group.messaging_retention import (
    PERSONA_AUDIT_EVENT_TYPES,
    PersonaRetentionCleaner,
    run_persona_retention,
)
from app.modules.owned_group.models_extra import OwnedGroupAuditEvent


class FakeRedis:
    def __init__(self, *, occupied: bool = False, lose_on_refresh: bool = False) -> None:
        self.values: dict[str, str] = {"occupied": "other"} if occupied else {}
        self.occupied = occupied
        self.lose_on_refresh = lose_on_refresh
        self.set_calls: list[tuple[str, str, bool, int]] = []
        self.eval_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def set(self, key: str, value: str, *, nx: bool, ex: int) -> bool | None:
        self.set_calls.append((key, value, nx, ex))
        if self.occupied or (nx and key in self.values):
            return None
        self.values[key] = value
        return True

    async def eval(self, script: str, _key_count: int, key: str, *args: Any) -> int:
        self.eval_calls.append((script, (key, *args)))
        token = str(args[0])
        if self.values.get(key) != token:
            return 0
        if "expire" in script:
            if self.lose_on_refresh:
                self.values.pop(key, None)
                return 0
            return 1
        if "del" in script:
            self.values.pop(key, None)
            return 1
        raise AssertionError(f"unexpected Lua script: {script}")


def _execution(
    *,
    suffix: str,
    status: str,
    updated_at: datetime,
    persona_snapshot: dict[str, Any] | None,
) -> GroupAccountMessageExecution:
    return GroupAccountMessageExecution(
        policy_id=1,
        owned_group_asset_id=1,
        core_group_id=1,
        telegram_chat_id=-10001,
        account_id=1,
        trigger_type="manual",
        message_purpose="community_ai",
        content_category="community",
        mode_snapshot="ai",
        policy_revision=1,
        persona_revision_snapshot=1 if persona_snapshot is not None else None,
        persona_source_snapshot="configured" if persona_snapshot is not None else None,
        persona_snapshot=persona_snapshot,
        persona_hash="a" * 64 if persona_snapshot is not None else None,
        prompt_template_version="owned-group-persona-v1" if persona_snapshot is not None else None,
        prompt_hash="b" * 64 if persona_snapshot is not None else None,
        governance_rules_hash="c" * 64 if persona_snapshot is not None else None,
        status=status,
        prompt_context={"must": "remain"},
        content="retained message",
        content_hash="d" * 64,
        idempotency_key=f"persona-retention-{suffix}",
        correlation_id=f"correlation-{suffix}",
        created_at=updated_at,
        updated_at=updated_at,
    )


def _audit(
    *,
    event_type: str,
    resource_type: str,
    created_at: datetime,
) -> OwnedGroupAuditEvent:
    return OwnedGroupAuditEvent(
        event_type=event_type,
        resource_type=resource_type,
        resource_id=1,
        result="success",
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_retention_cleans_only_old_terminal_snapshots_and_whitelisted_audits(
    test_db,
) -> None:
    now = datetime(2026, 9, 10, 8, 0, 0)
    old_execution = _execution(
        suffix="old-terminal",
        status="sent",
        updated_at=now - timedelta(days=91),
        persona_snapshot={"name": "must be cleared"},
    )
    nonterminal = _execution(
        suffix="old-nonterminal",
        status="ready_to_send",
        updated_at=now - timedelta(days=120),
        persona_snapshot={"name": "must remain"},
    )
    recent_terminal = _execution(
        suffix="recent-terminal",
        status="failed",
        updated_at=now - timedelta(days=89),
        persona_snapshot={"name": "must remain"},
    )
    exact_cutoff = _execution(
        suffix="exact-cutoff",
        status="cancelled",
        updated_at=now - timedelta(days=90),
        persona_snapshot={"name": "must remain"},
    )
    test_db.add_all([old_execution, nonterminal, recent_terminal, exact_cutoff])

    removable_account_audit = _audit(
        event_type="account_persona_updated",
        resource_type="account_persona",
        created_at=now - timedelta(days=366),
    )
    removable_setting_audit = _audit(
        event_type="account_persona_feature_updated",
        resource_type="persona_setting",
        created_at=now - timedelta(days=500),
    )
    unrelated_event = _audit(
        event_type="message_execution_failed",
        resource_type="account_persona",
        created_at=now - timedelta(days=500),
    )
    unrelated_resource = _audit(
        event_type="account_persona_updated",
        resource_type="message_execution",
        created_at=now - timedelta(days=500),
    )
    recent_persona_audit = _audit(
        event_type="account_persona_reset",
        resource_type="account_persona",
        created_at=now - timedelta(days=364),
    )
    test_db.add_all(
        [
            removable_account_audit,
            removable_setting_audit,
            unrelated_event,
            unrelated_resource,
            recent_persona_audit,
        ]
    )
    await test_db.commit()
    ids = {
        "old": old_execution.id,
        "nonterminal": nonterminal.id,
        "recent": recent_terminal.id,
        "exact": exact_cutoff.id,
    }
    audit_ids_to_keep = {unrelated_event.id, unrelated_resource.id, recent_persona_audit.id}

    redis = FakeRedis()
    result = await run_persona_retention(
        test_db,
        redis_client=redis,
        now=now,
        batch_size=1,
    )

    assert result == {
        "scanned": 3,
        "updated": 1,
        "deleted": 2,
        "failed": 0,
        "would_update": 1,
        "would_delete": 2,
        "execution_scanned": 1,
        "audit_scanned": 2,
        "dry_run": False,
        "lock_acquired": True,
        "skipped_locked": False,
    }
    rows = {
        row.id: row
        for row in (
            await test_db.execute(
                select(GroupAccountMessageExecution).where(
                    GroupAccountMessageExecution.id.in_(ids.values())
                )
            )
        )
        .scalars()
        .all()
    }
    assert rows[ids["old"]].persona_snapshot is None
    assert rows[ids["old"]].prompt_context == {"must": "remain"}
    assert rows[ids["old"]].content == "retained message"
    assert rows[ids["old"]].persona_hash == "a" * 64
    assert rows[ids["old"]].prompt_hash == "b" * 64
    assert rows[ids["old"]].governance_rules_hash == "c" * 64
    assert rows[ids["nonterminal"]].persona_snapshot == {"name": "must remain"}
    assert rows[ids["recent"]].persona_snapshot == {"name": "must remain"}
    assert rows[ids["exact"]].persona_snapshot == {"name": "must remain"}

    remaining_audit_ids = set(
        (
            await test_db.execute(
                select(OwnedGroupAuditEvent.id).order_by(OwnedGroupAuditEvent.id)
            )
        )
        .scalars()
        .all()
    )
    assert remaining_audit_ids == audit_ids_to_keep
    assert redis.values == {}
    assert any("expire" in script for script, _args in redis.eval_calls)
    assert any("del" in script for script, _args in redis.eval_calls)


@pytest.mark.asyncio
async def test_retention_dry_run_counts_candidates_without_writing(test_db) -> None:
    now = datetime(2026, 9, 10, 8, 0, 0)
    execution = _execution(
        suffix="dry-run",
        status="rejected",
        updated_at=now - timedelta(days=91),
        persona_snapshot={"name": "keep in dry run"},
    )
    audit = _audit(
        event_type="account_persona_preview_failed",
        resource_type="account_persona",
        created_at=now - timedelta(days=366),
    )
    test_db.add_all([execution, audit])
    await test_db.commit()

    result = await run_persona_retention(
        test_db,
        redis_client=FakeRedis(),
        now=now,
        dry_run=True,
    )

    assert result["scanned"] == 2
    assert result["would_update"] == 1
    assert result["would_delete"] == 1
    assert result["updated"] == 0
    assert result["deleted"] == 0
    assert result["failed"] == 0
    assert (await test_db.get(GroupAccountMessageExecution, execution.id)).persona_snapshot == {
        "name": "keep in dry run"
    }
    assert await test_db.get(OwnedGroupAuditEvent, audit.id) is not None


@pytest.mark.asyncio
async def test_retention_skips_when_distributed_lock_is_occupied(test_db) -> None:
    now = datetime(2026, 9, 10, 8, 0, 0)
    execution = _execution(
        suffix="locked",
        status="expired",
        updated_at=now - timedelta(days=91),
        persona_snapshot={"name": "keep while another task owns lock"},
    )
    test_db.add(execution)
    await test_db.commit()

    result = await run_persona_retention(
        test_db,
        redis_client=FakeRedis(occupied=True),
        now=now,
    )

    assert result["lock_acquired"] is False
    assert result["skipped_locked"] is True
    assert result["scanned"] == 0
    assert (await test_db.get(GroupAccountMessageExecution, execution.id)).persona_snapshot == {
        "name": "keep while another task owns lock"
    }


@pytest.mark.asyncio
async def test_retention_fails_closed_if_lock_lease_is_lost(test_db) -> None:
    now = datetime(2026, 9, 10, 8, 0, 0)
    execution = _execution(
        suffix="lease-lost",
        status="skipped",
        updated_at=now - timedelta(days=91),
        persona_snapshot={"name": "keep after lease loss"},
    )
    test_db.add(execution)
    await test_db.commit()

    result = await run_persona_retention(
        test_db,
        redis_client=FakeRedis(lose_on_refresh=True),
        now=now,
    )

    assert result["failed"] == 1
    assert result["updated"] == 0
    assert (await test_db.get(GroupAccountMessageExecution, execution.id)).persona_snapshot == {
        "name": "keep after lease loss"
    }


def test_retention_batch_size_is_capped_at_500() -> None:
    with pytest.raises(ValueError, match="between 1 and 500"):
        PersonaRetentionCleaner(object(), batch_size=501)  # type: ignore[arg-type]


def test_persona_audit_event_allowlist_is_exact() -> None:
    assert PERSONA_AUDIT_EVENT_TYPES == {
        "account_persona_created",
        "account_persona_updated",
        "account_persona_reset",
        "account_persona_preview_generated",
        "account_persona_preview_failed",
        "account_persona_feature_updated",
    }


def test_retention_celery_task_and_daily_schedule_are_registered() -> None:
    from app.celery import celery_app
    from app.modules.owned_group.messaging_retention_tasks import (
        cleanup_owned_group_persona_retention,
    )

    task_name = (
        "app.modules.owned_group.messaging_retention_tasks."
        "cleanup_owned_group_persona_retention"
    )
    assert cleanup_owned_group_persona_retention.name == task_name
    assert task_name in celery_app.tasks
    assert celery_app.conf.beat_schedule["owned-group-persona-retention-daily"]["task"] == task_name
    assert celery_app.conf.beat_schedule["owned-group-persona-retention-daily"]["kwargs"] == {
        "batch_size": 500,
        "dry_run": False,
    }
    assert celery_app.conf.task_routes[task_name] == {"queue": "owned_group"}
