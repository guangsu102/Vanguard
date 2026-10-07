import asyncio
import json
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import types

from app.modules.acquisition import qualification_ai as ai
from app.modules.acquisition.automation import GroupAdRulesAuditResult
from app.modules.acquisition.qualification_service import review_schedule, qualifies_for_automatic_exit
from app.modules.acquisition.read_costs import COST_VERSION, estimate
from app.modules.acquisition.execution_admission import available_slots
from tests.integration.test_growth_account_scheduler import queue_store  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["unknown", "exception", "cancelled", "allowed"])
async def test_identical_material_has_one_provider_attempt_even_after_restart(test_db, outcome):
    snapshot = {"raw_peer_id": 42, "evidence": [{"source": "full_about", "text": "rules"}]}
    account = Obj(id=2, profile_bio="bio")
    result = GroupAdRulesAuditResult(ad_allowed=outcome == "allowed", confidence=99,
                                     policy_mode="soft_ad_allowed" if outcome == "allowed" else "unknown")
    call = AsyncMock(return_value=result)
    if outcome in {"exception", "cancelled"}:
        call.side_effect = RuntimeError("offline") if outcome == "exception" else asyncio.CancelledError()
    service = Obj(db=test_db, _ad_policy_llm=lambda: None, _evaluate_group_ad_rules_with_ai=call)
    if outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await ai.review_semantics(service, snapshot, account, GroupAdRulesAuditResult(), {})
    else:
        await ai.review_semantics(service, snapshot, account, GroupAdRulesAuditResult(), {})
    snapshot["collected_at"] = (datetime.utcnow() + timedelta(days=3)).isoformat()
    account.spam_checked_at = datetime.utcnow()
    service = Obj(**vars(service))
    cached = await ai.review_semantics(service, snapshot, account, GroupAdRulesAuditResult(), {})
    assert call.await_count == 1
    assert cached.ad_allowed is (outcome == "allowed")
    assert snapshot["ai_decision"] == ("pass" if outcome == "allowed" else "fail")
    assert snapshot["ai_final"] and not snapshot["ai_pending"]


@pytest.mark.asyncio
async def test_existing_failed_material_is_adopted_without_repeating_provider_call(test_db):
    account = Obj(id=2, profile_bio="bio")
    old = {"raw_peer_id": 42, "evidence": [{"source": "full_about", "text": "rules"}],
           "profile_fingerprint": ai.profile_fingerprint(account),
           "advertising_audit": {"reason": "group_rules_ai_provider_content_rejected"}}
    service = Obj(db=test_db, _ad_policy_llm=lambda: None, _evaluate_group_ad_rules_with_ai=AsyncMock())
    result = await ai.review_semantics(service, deepcopy(old), account, GroupAdRulesAuditResult(), {}, previous=old)
    assert result.ad_allowed is False and result.cache_hit
    service._evaluate_group_ad_rules_with_ai.assert_not_awaited()


def test_unknown_ai_is_terminal_failure_without_fabricating_a_group_ban():
    now = datetime.utcnow()
    snapshot = {"decision": "observe", "reason": "group_rules_ai_unavailable", "ai_final": True,
                "ai_decision": "fail", "collection_progress": {"pending_message_ids": [1, 2]}}
    assert review_schedule(snapshot, {}, now) == ("reject", "group_rules_ai_unavailable", "completed", None)
    assert not qualifies_for_automatic_exit(snapshot)


@pytest.mark.asyncio
async def test_failed_ai_does_not_create_rejoin_ban_but_real_ban_is_preserved(test_db):
    from tests.unit.test_qualification_service import setup
    from app.modules.acquisition.qualification_join_gate import rejection_facts
    _, _, _, row = await setup(test_db, decision="reject")
    snapshot = {"ai_final": True, "ai_decision": "fail", "decision": "reject"}
    row.evidence_json = json.dumps(snapshot)
    assert rejection_facts(row) == []
    snapshot["group_level_advertising_ban"] = True
    row.evidence_json = json.dumps(snapshot)
    assert len(rejection_facts(row)) == 1


def test_cost_forecast_uses_current_path_and_includes_failed_work():
    now = time.time()
    samples = [json.dumps({"at": now, "reads": 11, "complete": True})] * 110
    samples += [json.dumps({"at": now, "reads": 4, "complete": True, "path": COST_VERSION})] * 8
    assert estimate(samples, 6, now, path=COST_VERSION) == (5, 8, True)
    samples.append(json.dumps({"at": now, "reads": 24, "complete": False, "path": COST_VERSION}))
    assert estimate(samples, 6, now, path=COST_VERSION) == (9, 8, True)
    assert estimate(samples, 6, now + 7 * 3600, path=COST_VERSION) == (6, 0, False)


def test_thirty_accounts_cannot_admit_thirty_new_groups_into_four_review_slots():
    admitted = 0
    for _ in range(30):
        if available_slots(admitted):
            admitted += 1
    assert admitted == 4
    assert available_slots(4) == 0
    assert available_slots(0, 120) == 2


@pytest.mark.asyncio
async def test_short_batch_reuses_connection_but_drops_lost_lease():
    from app.modules.acquisition.review_batch import ReviewBatch
    first = Obj(client=Obj(is_connected=Mock(return_value=True)), operation_lease_lost=False)
    second = Obj(client=Obj(is_connected=Mock(return_value=True)), operation_lease_lost=False)
    pool = Obj(acquire_by_id=AsyncMock(side_effect=[first, second]), release=AsyncMock())
    batch = ReviewBatch(pool)
    assert await batch.acquire(2) is first
    assert await batch.acquire(2) is first
    assert pool.acquire_by_id.await_count == 1
    first.operation_lease_lost = True
    assert await batch.acquire(2) is second
    await batch.close()
    assert pool.release.await_count == 2


@pytest.mark.asyncio
async def test_request_id_and_receipt_are_durable_before_sdk_conversion(test_db, monkeypatch):
    from app.core.account import send_receipts as receipts
    from app.core.settings_models import SystemSetting
    @asynccontextmanager
    async def session():
        yield test_db
        await test_db.commit()
    monkeypatch.setattr("app.core.database.get_db_session", session)
    from telethon.tl.functions.messages import SendMessageRequest
    request = SendMessageRequest(types.InputPeerChannel(42, 123), "test", random_id=456)
    token = receipts.receipt_scope.set((2, "ad:test"))
    try:
        await receipts.record_request(2, [request])
        row = await test_db.get(SystemSetting, receipts.receipt_key(2, "ad:test"))
        assert json.loads(row.value)["random_id"] == 456
        response = types.Updates([types.UpdateMessageID(77, 456)], [], [], datetime.utcnow(), 1)
        await receipts.record_request(2, [request], response)
        assert json.loads(row.value)["message_id"] == 77
        request.random_id = 999
        with pytest.raises(RuntimeError, match="original_request_changed"):
            await receipts.record_request(2, [request])
    finally:
        receipts.receipt_scope.reset(token)
    assert receipts.response_id(request, response) is None


@pytest.mark.asyncio
async def test_sync_recovery_uses_bounded_existing_reserve_and_honors_cooldown(queue_store, test_db, monkeypatch):
    from app.core.account import rpc_governor as rpc
    @asynccontextmanager
    async def session():
        yield test_db
    monkeypatch.setattr("app.core.database.get_db_session", session)
    monkeypatch.setattr("app.core.redis.get_redis", AsyncMock(return_value=queue_store))
    # Consume normal sync plus the elastic share, leaving other lanes intact.
    await queue_store.set("vanguard:rpc:2:day", 650, ex=3600)
    await queue_store.set("vanguard:rpc:2:sync_reserved_day", 650, ex=3600)
    governor = rpc.RpcGovernor(2, lambda: "growth_listener")
    await governor.before(["updates.GetDifferenceRequest"], sync=True)
    assert int(await queue_store.get("vanguard:rpc:2:day")) == 651
    assert int(await queue_store.get("vanguard:rpc:2:listener_day")) == 1
    assert await queue_store.get("vanguard:rpc:2:ad_day") is None
    await queue_store.set("vanguard:rpc:cooldown:2", 1, ex=120)
    with pytest.raises(rpc.RpcDeferred, match="telegram_rpc_cooldown"):
        await governor.before(["updates.GetDifferenceRequest"], sync=True)
    assert int(await queue_store.get("vanguard:rpc:2:day")) == 651


def test_probe_and_default_routes_have_consumers():
    from app.celery import celery_app
    assert celery_app.conf.task_default_queue == "default"
    assert celery_app.amqp.router.route({}, "app.core.scheduler.tasks.auto_probe_unknown_group_ad_policies_task")["queue"].name == "automation"
    assert "auto-probe-unknown-ad-policies-every-5min" not in celery_app.conf.beat_schedule


@pytest.mark.asyncio
async def test_historical_unknown_ai_is_closed_without_ai_or_new_telegram_evidence(test_db):
    from tests.unit.test_qualification_service import setup
    from app.core.settings_models import SystemSetting
    account, _, member, row = await setup(test_db, decision="observe")
    material = {"raw_peer_id": 42, "evidence": [{"source": "full_about", "text": "rules"}],
                "profile_fingerprint": ai.profile_fingerprint(account),
                "advertising_audit": {"reason": "group_rules_ai_unavailable"}}
    row.evidence_json = json.dumps(material)
    row.next_retry_at = datetime.utcnow()
    await test_db.commit()
    assert (await ai.retire_failed_retries(test_db))["retired_ai_retries"] == [row.id]
    assert row.decision == "reject" and row.next_retry_at is None
    assert member.ad_status == "blocked"
    assert not qualifies_for_automatic_exit(json.loads(row.evidence_json))
    key = "qualification.ai.once." + ai.semantic_key(material, [], account, {})
    assert await test_db.get(SystemSetting, key) is not None


@pytest.mark.asyncio
async def test_no_id_receipt_remains_unknown_and_explicitly_requires_evidence(test_db):
    from tests.unit.test_qualification_service import setup
    from app.modules.acquisition.models import AdDeliveryLog
    from app.core.account.send_receipts import recover_missing_receipts
    _, group, _, _ = await setup(test_db)
    now = datetime.utcnow()
    log = AdDeliveryLog(account_id=2, group_id=group.id, telegram_group_id=group.group_id,
                        ad_campaign_id=1, status="pending", created_at=now-timedelta(days=1))
    test_db.add(log)
    await test_db.commit()
    result = await recover_missing_receipts(test_db, now, 2, 10)
    assert result == {
        "receipt_ids_recovered": 0,
        "receipt_evidence_required": 0,
        "manual_evidence_required": 0,
        "abandoned_pending_closed": 0,
    }
    assert log.status == "pending" and log.telegram_message_id is None
    assert log.qualification_context_json is None


@pytest.mark.asyncio
async def test_receipt_recheck_timer_does_not_become_survival_reader_due(test_db, monkeypatch):
    from tests.unit.test_review_throughput import parked_scenario
    from app.core.settings_models import SystemSetting
    from app.core.account.send_receipts import recover_missing_receipts, RECHECK_PREFIX
    from app.core.account import rpc_governor as rpc
    from tests.unit.test_ad_read_reserve import mock_usage

    account, _, log = await parked_scenario(test_db)
    now = datetime.utcnow()
    await recover_missing_receipts(test_db, now, account.id, 10)
    await test_db.refresh(log)
    assert log.survival_check_due_at is None
    assert await test_db.get(SystemSetting, RECHECK_PREFIX + str(log.id)) is None
    mock_usage(monkeypatch, {"hour": (120, 1800), "day": (1200, 60000)})
    state = await rpc.snapshot(test_db, account.id, now)
    assert state["survival_lending"]["holds"]["hour"] == 20


@pytest.mark.asyncio
async def test_legacy_receipt_evidence_marker_bypasses_future_survival_timer(test_db):
    from tests.unit.test_qualification_service import setup
    from app.core.account.send_receipts import recover_missing_receipts
    from app.modules.acquisition.models import AdDeliveryLog

    _, group, _, _ = await setup(test_db)
    now = datetime.utcnow()
    log = AdDeliveryLog(
        account_id=2,
        group_id=group.id,
        telegram_group_id=group.group_id,
        ad_campaign_id=1,
        status="pending",
        created_at=now - timedelta(days=1),
        survival_status="not_required",
        survival_stage="complete",
        survival_check_due_at=now + timedelta(hours=6),
        qualification_context_json=json.dumps({
            "send_reconciliation": {
                "state": "receipt_evidence_required",
                "scope": "account_group",
                "automatic_resend": False,
            },
        }),
    )
    test_db.add(log)
    await test_db.commit()

    result = await recover_missing_receipts(test_db, now, 2, 10)

    assert result == {
        "receipt_ids_recovered": 0,
        "receipt_evidence_required": 0,
        "manual_evidence_required": 1,
        "abandoned_pending_closed": 0,
    }
    await test_db.refresh(log)
    assert log.survival_check_due_at is None
    assert log.status == "failed"
    assert json.loads(log.qualification_context_json)["send_reconciliation"]["state"] == "manual_evidence_required"


@pytest.mark.asyncio
async def test_parked_unknown_receipt_does_not_count_as_account_blocking_reconciliation(test_db):
    from tests.unit.test_qualification_service import setup
    from app.core.account.models import AccountOutboundAttempt
    from app.core.runtime_health import outbound_reconciliation_counts
    from app.modules.acquisition.models import AdCampaign, AdDeliveryLog

    account, group, _, _ = await setup(test_db)
    campaign = AdCampaign(name="parked-receipt-health")
    test_db.add(campaign)
    await test_db.flush()
    now = datetime.utcnow()
    token = "parked-token"
    test_db.add_all(
        [
            AccountOutboundAttempt(
                account_id=account.id,
                attempt_key="ad:" + token,
                category="ad",
                target_key=str(group.group_id),
                state="unknown",
                attempted_at=now - timedelta(days=1),
                completed_at=now - timedelta(days=1),
                error_code="TelegramSendOutcomeUnknownError",
                context_json="{}",
            ),
            AdDeliveryLog(
                account_id=account.id,
                group_id=group.id,
                telegram_group_id=group.group_id,
                ad_campaign_id=campaign.id,
                reservation_token=token,
                status="pending",
                error="send_outcome_unknown:ValueError",
                created_at=now - timedelta(days=1),
                survival_status="not_required",
                survival_stage="complete",
                qualification_context_json=json.dumps(
                    {
                        "send_reconciliation": {
                            "state": "receipt_evidence_required",
                            "scope": "account_group",
                            "automatic_resend": False,
                        }
                    }
                ),
            ),
            AccountOutboundAttempt(
                account_id=account.id,
                attempt_key="ad:unmatched-token",
                category="ad",
                target_key="another-group",
                state="unknown",
                attempted_at=now - timedelta(days=1),
                completed_at=now - timedelta(days=1),
                error_code="TelegramSendOutcomeUnknownError",
                context_json="{}",
            ),
        ]
    )
    await test_db.commit()

    assert await outbound_reconciliation_counts(test_db, now) == (2, 1, 1)
