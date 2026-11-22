import json
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.account import reconciliation_stream as stream
from app.core.account.control_models import ReconciliationTask
from app.core.account.models import AccountOutboundAttempt
from app.core.account.reconciliation_handlers import execute_task
from app.core.account.send_receipts import receipt_key
from app.core.config import settings
from app.core.settings_models import SystemSetting
from app.modules.acquisition import qualification_ai as ai
from app.modules.acquisition.automation import GroupAdRulesAuditResult
from app.modules.acquisition.models import AdDeliveryLog, AdDeliveryScheduleState
from app.modules.acquisition.qualification_identity import peer_identity
from app.modules.acquisition.qualification_service import review_schedule
from tests.unit.test_qualification_service import setup


def material():
    return {
        "raw_peer_id": 42,
        "group_type": "supergroup",
        "evidence": [{"source": "full_about", "text": "Members may advertise"}],
    }


def accepted():
    return GroupAdRulesAuditResult(ad_allowed=True, policy_mode="soft_ad_allowed", confidence=99)


@pytest.mark.asyncio
async def test_capacity_wait_does_not_spend_material_attempt(test_db, monkeypatch):
    call = AsyncMock(return_value=accepted())
    service = Obj(db=test_db, _ad_policy_llm=lambda: Obj(), _evaluate_group_ad_rules_with_ai=call)
    account = Obj(id=2, profile_bio="")
    snapshot = material()
    slot = ai.claim_slot
    monkeypatch.setattr(ai, "claim_slot", AsyncMock(return_value=None))
    result = await ai.review_semantics(service, snapshot, account, GroupAdRulesAuditResult(), {})
    assert result.reason == "group_rules_ai_capacity_unavailable"
    assert (
        await test_db.scalar(
            select(SystemSetting).where(SystemSetting.key.like("qualification.ai.once.%"))
        )
        is None
    )
    call.assert_not_awaited()
    snapshot.update(reason=result.reason, decision="reject")
    now = datetime.utcnow()
    assert review_schedule(snapshot, {"technical_failures": 2}, now) == (
        "observe",
        result.reason,
        "waiting_ai",
        now + timedelta(seconds=60),
    )
    assert snapshot["technical_failures"] == 2 and not snapshot["ai_final"]
    monkeypatch.setattr(ai, "claim_slot", slot)
    assert (
        await ai.review_semantics(service, snapshot, account, GroupAdRulesAuditResult(), {})
    ).ad_allowed
    await ai.review_semantics(service, snapshot, account, GroupAdRulesAuditResult(), {})
    assert call.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase,expected_calls", [(None, 1), ("attempted", 0), ("completed", 0)])
async def test_only_proven_legacy_preflight_cache_can_be_reclaimed(test_db, phase, expected_calls):
    account = Obj(id=2, profile_bio="")
    snapshot = material()
    key = "qualification.ai.once." + ai.semantic_key(snapshot, [], account, {})
    data = {
        "result": ai.saved_result(
            GroupAdRulesAuditResult(
                ad_allowed=False, reason="group_rules_ai_destination_unapproved"
            )
        )
    }
    if phase:
        data["phase"] = phase
    test_db.add(SystemSetting(key=key, value=json.dumps(data)))
    await test_db.commit()
    call = AsyncMock(return_value=accepted())
    service = Obj(db=test_db, _ad_policy_llm=lambda: Obj(), _evaluate_group_ad_rules_with_ai=call)
    result = await ai.review_semantics(service, snapshot, account, GroupAdRulesAuditResult(), {})
    assert call.await_count == expected_calls
    assert result.ad_allowed is bool(expected_calls)
    await ai.review_semantics(service, snapshot, account, GroupAdRulesAuditResult(), {})
    assert call.await_count == expected_calls


@pytest.mark.asyncio
async def test_reader_during_provider_call_cannot_reclaim_material(test_db):
    account = Obj(id=2, profile_bio="")
    other = Obj(
        db=test_db, _ad_policy_llm=lambda: Obj(), _evaluate_group_ad_rules_with_ai=AsyncMock()
    )

    async def evaluate(*args):
        result = await ai.review_semantics(
            other, material(), account, GroupAdRulesAuditResult(), {}
        )
        assert result.reason == "group_rules_ai_attempt_interrupted" and result.cache_hit
        return accepted()

    service = Obj(
        db=test_db,
        _ad_policy_llm=lambda: Obj(),
        _evaluate_group_ad_rules_with_ai=AsyncMock(side_effect=evaluate),
    )
    assert (
        await ai.review_semantics(service, material(), account, GroupAdRulesAuditResult(), {})
    ).ad_allowed
    other._evaluate_group_ad_rules_with_ai.assert_not_awaited()


@pytest.mark.parametrize(
    "url",
    [
        "http://pipenai.xyz/v1",
        "https://pipenai.xyz.attacker.test/v1",
        "https://pipenai.xyz:444/v1",
        "https://user@pipenai.xyz/v1",
        "https://pipenai.xyz/other",
        "https://pipenai.xyz/v1?redirect=elsewhere",
        "https://ark.cn-beijing.volces.com/api/coding/v3/v1",
    ],
)
def test_destination_scope_stays_exact(url):
    assert not ai.approved_destination(url)


@pytest.mark.asyncio
async def test_recovery_wakes_preflight_failure_once_and_preserves_real_failures(test_db):
    account, _, member, row = await setup(test_db, decision="reject")
    snapshot = material()
    reason = "group_rules_ai_destination_unapproved"
    snapshot.update(
        ai_final=True,
        ai_decision="fail",
        ai_pending=False,
        profile_fingerprint=ai.profile_fingerprint(account),
        advertising_audit={"reason": reason, "ai_reviews": []},
    )
    row.reason, row.evidence_json = reason, json.dumps(snapshot)
    row.next_retry_at = None
    key = "qualification.ai.once." + ai.semantic_key(snapshot, [], account, {})
    old_value = json.dumps(
        {"result": ai.saved_result(GroupAdRulesAuditResult(ad_allowed=False, reason=reason))}
    )
    test_db.add(SystemSetting(key=key, value=old_value))
    await test_db.commit()
    config = {"enabled": True, "account_ids": [account.id]}
    assert await ai.recover_preflight_reviews(test_db, config) == [row.id]
    assert row.state == "queued" and row.decision == "observe" and member.ad_status == "blocked"
    assert await ai.recover_preflight_reviews(test_db, config) == []
    assert (await test_db.get(SystemSetting, key)).value == old_value
    assert json.loads(row.evidence_json)["ai_preflight_recovery"]["reason"] == reason
    row.reason = "group_rules_ai_unavailable"
    snapshot["advertising_audit"]["reason"] = row.reason
    row.evidence_json, row.state = json.dumps(snapshot), "completed"
    await test_db.commit()
    assert await ai.recover_preflight_reviews(test_db, config) == []


async def old_ad(test_db):
    account, group, _, _ = await setup(test_db)
    now = datetime.utcnow()
    token = "old-send"
    attempt = AccountOutboundAttempt(
        account_id=account.id,
        attempt_key="ad:" + token,
        category="ad",
        target_key=str(group.group_id),
        state="unknown",
        created_at=now - timedelta(days=2),
        attempted_at=now - timedelta(days=2),
        error_code="TelegramSendOutcomeUnknownError",
    )
    log = AdDeliveryLog(
        account_id=account.id,
        group_id=group.id,
        telegram_group_id=group.group_id,
        ad_campaign_id=1,
        status="failed",
        reservation_token=token,
        created_at=now - timedelta(days=2),
        error="manual_evidence_required:send_outcome_unknown:message_id_unproven",
        qualification_context_json=json.dumps(
            {"send_reconciliation": {"state": "manual_evidence_required"}}
        ),
    )
    schedule = AdDeliveryScheduleState(
        account_id=account.id,
        campaign_id=1,
        group_id=group.id,
        telegram_group_id=group.group_id,
        status="paused",
        next_due_at=now,
        last_reason="outbound_target_reconciliation_required:1",
    )
    test_db.add_all([attempt, log, schedule])
    await test_db.commit()
    return attempt, log, schedule


def enable_reconciliation(monkeypatch):
    monkeypatch.setattr(settings, "RECONCILIATION_STREAM_ENABLED", True)
    monkeypatch.setattr(settings, "RECONCILIATION_SHADOW_MODE", False)
    monkeypatch.setattr(settings, "RECONCILIATION_ACCOUNT_IDS", "2")


@pytest.mark.asyncio
async def test_ledger_scan_reaches_failed_log_and_parks_without_releasing_fence(
    test_db, monkeypatch
):
    enable_reconciliation(monkeypatch)
    attempt, log, schedule = await old_ad(test_db)
    assert await stream.scan_database(test_db) == 1
    task = await test_db.scalar(select(ReconciliationTask))
    assert task.source_id == log.id and task.verification_due_at == log.created_at
    await stream.claim_task(test_db, task.task_key, "test")
    result = await execute_task(test_db, task.id, "test", 2, account_pool=Obj())
    assert result["state"] == "manual_required"
    assert await stream.finish_task(
        test_db, task.task_key, "test", state=result["state"], error_code=result["error"],
        result=result,
    )
    assert attempt.state == "unknown" and attempt.message_id is None
    assert schedule.status == "paused" and log.status == "failed"
    assert json.loads(task.evidence_json)["automatic_resend"] is False
    assert await stream.scan_database(test_db) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("proof_kind", ["exact", "recorded_log", "wrong_account", "wrong_peer", "conflict"])
async def test_exact_receipt_is_required_to_resolve_ledger(test_db, monkeypatch, proof_kind):
    from app.core import metrics

    enable_reconciliation(monkeypatch)
    monkeypatch.setattr(metrics, "record_delivery", AsyncMock())
    attempt, log, schedule = await old_ad(test_db)
    proof = {
        "account_id": attempt.account_id,
        "peer": list(peer_identity(log.telegram_group_id)),
        "message_id": 99,
    }
    # Original requests carry a concrete Telegram namespace.
    proof["peer"][1] = proof["peer"][1] or "channel"
    if proof_kind == "wrong_account":
        proof["account_id"] += 1
    elif proof_kind == "wrong_peer":
        proof["peer"][0] += 1
    elif proof_kind == "conflict":
        log.telegram_message_id = 100
    elif proof_kind == "recorded_log":
        log.telegram_message_id = 99
    test_db.add(
        SystemSetting(
            key=receipt_key(attempt.account_id, attempt.attempt_key), value=json.dumps(proof)
        )
    )
    await test_db.commit()
    await stream.scan_database(test_db)
    task = await test_db.scalar(select(ReconciliationTask))
    await stream.claim_task(test_db, task.task_key, "test")
    outcome = await execute_task(test_db, task.id, "test", 2, account_pool=Obj())
    await test_db.refresh(attempt)
    await test_db.refresh(log)
    await test_db.refresh(schedule)
    if proof_kind in {"exact", "recorded_log"}:
        assert outcome["state"] == "succeeded"
        assert attempt.state == "succeeded" and attempt.message_id == log.telegram_message_id == 99
        assert log.status == "success" and schedule.status == "idle"
    else:
        assert outcome["state"] == "manual_required"
        assert attempt.state == "unknown" and schedule.status == "paused"
