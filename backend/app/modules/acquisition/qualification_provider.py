"""Finish a claimed once-only AI request without owning a Telegram work slot."""
from __future__ import annotations

import asyncio
from contextvars import Context
from copy import copy, deepcopy
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

TASKS: set[asyncio.Task] = set()


def pending_result(snapshot: dict, evidence: list, data: dict) -> Any:
    """An in-flight marker is waiting; an expired/crashed marker never retries."""
    from app.modules.acquisition.qualification_service import _date
    if (data.get("phase") != "attempted" or data.get("background") is not True
            or (_date(data.get("deadline_at")) or datetime.min) <= datetime.utcnow()):
        return None
    from app.modules.acquisition.automation import GroupAdRulesAuditResult
    snapshot.update(ai_final=False, ai_pending=True, ai_decision=None,
        ai_request_started=True, ai_provider_deadline_at=data["deadline_at"], ai_retry_after_seconds=30)
    return GroupAdRulesAuditResult(ad_allowed=False, reason="group_rules_ai_pending", evidence=evidence)


def launch(service: Any, key: str, claim: tuple[str, str], material: str, account_id: int,
           evidence: list, local: Any, limits: dict) -> None:
    # A clean context carries neither session owner leases nor read batch/work
    # state. The global strong reference survives completion of the owner call.
    task = asyncio.create_task(_run(service, key, claim, material, account_id,
        deepcopy(evidence), deepcopy(local), deepcopy(limits)), context=Context(),
        name="qualification-provider-"+material[:12])
    TASKS.add(task)
    task.add_done_callback(_finished)


def _finished(task: asyncio.Task) -> None:
    TASKS.discard(task)
    if not task.cancelled():
        task.exception()  # Retrieve failures; the durable attempted marker stays closed.


async def _run(service: Any, key: str, claim: tuple[str, str], material: str,
               account_id: int, evidence: list, local: Any, limits: dict) -> None:
    from app.modules.acquisition.automation import GroupAdRulesAuditResult
    from app.modules.acquisition.qualification_ai import binary_result, finish_once, release_slot
    bind = service.db.bind
    result = GroupAdRulesAuditResult(ad_allowed=False, reason="group_rules_ai_attempt_interrupted")
    cancelled = False
    try:
        # The evaluator uses its own service/session and no Telegram methods.
        # Opening AsyncSession does not check out a connection; the real
        # evaluator's provider call is database-free throughout its await.
        async with AsyncSession(bind=bind, expire_on_commit=False) as provider_db:
            detached = copy(service)
            detached.db = provider_db
            async with asyncio.timeout(240):
                result = await detached._evaluate_group_ad_rules_with_ai(evidence, local, limits)
            result = binary_result(result, limits)
    except asyncio.CancelledError:
        cancelled = True
    except Exception:
        result = GroupAdRulesAuditResult(ad_allowed=False, reason="group_rules_ai_unavailable")
    finally:
        # Cache publication and Work wake-up use separate short transactions;
        # no cache lock is held while acquiring membership -> Work locks.
        try:
            async with AsyncSession(bind=bind, expire_on_commit=False) as db:
                await finish_once(db, key, result, {}, [])
                await release_slot(db, claim)
            await wake_material(bind, account_id, material)
        except Exception:
            # A durable attempted marker cannot repurchase the recognition.
            # Its bounded deadline yields an interrupted terminal result.
            pass
    if cancelled:
        raise asyncio.CancelledError


async def wake_material(bind: Any, account_id: int, material: str) -> None:
    from app.modules.acquisition import qualification_engine as engine
    from app.modules.acquisition.models import QualificationWork
    now = datetime.utcnow()
    async with AsyncSession(bind=bind, expire_on_commit=False) as db:
        candidates = (await db.scalars(select(QualificationWork).where(
            QualificationWork.account_id == account_id, QualificationWork.engine_version == engine.ENGINE_VERSION,
            QualificationWork.family == engine.FAMILY, QualificationWork.state == "WAIT_PROVIDER",
            QualificationWork.progress_json.contains(material),
        ))).all()
        for candidate in candidates:
            member = await engine._member_lock(db, candidate.membership_id)
            work = await db.get(QualificationWork, candidate.id, with_for_update=True, populate_existing=True)
            progress = engine._object(work.progress_json)
            progress = progress.get("pending_collection") or progress
            if (member is None or work.state != "WAIT_PROVIDER" or progress.get("ai_material_key") != material
                    or member.status != "joined" or member.left_at is not None
                    or member.joined_at != work.membership_joined_at
                    or work.scope_key != (await engine._scope(db, member))[0]):
                continue
            work.state, work.ready_at, work.reason, work.updated_at = "READY", now, "provider_completed", now
            from app.modules.acquisition import qualification_sla_runtime
            qualification_sla_runtime.stage_ready(db, work, dependency_ready_verified="provider_completed")
        engine._stage_wake(db, account_id, ready_at=now)
        await db.commit()
