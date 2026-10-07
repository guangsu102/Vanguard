"""V2 durable AI/verification ownership and fail-closed execution contracts."""

import copy
import json
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app.core.account.telegram_execution import TelegramSendPreflightError
from app.core.settings_models import SystemSetting
from app.modules.acquisition import qualification_ai as ai
from app.modules.acquisition import qualification_service as qualification
from app.modules.acquisition import qualification_verification as verification
from app.modules.acquisition.automation import GroupAdRulesAuditResult
from app.modules.acquisition.group_qualification import POLICY_VERSION, trial_evidence
from app.modules.acquisition.models import GroupQualificationAudit
from tests.unit.test_qualification_service import setup
from tests.unit.test_qualification_verification import setup as verification_setup


def evidence():
    return [
        {
            "source": "full_about",
            "text": "Members may advertise; no spam",
            "sender_role": "group_metadata",
        },
        {
            "source": "recent_promotional_message",
            "text": "Sale contact @seller_one https://one.example 13812345678",
            "sender_id": 998811,
            "sender_role": "ordinary",
            "message_id": 789123,
            "age_hours": 25,
            "accessible": True,
            "warning_search_complete": True,
            "promotion_key": "first",
            "promotion_targets": ["https://one.example"],
            "warning_reply_ids": [],
        },
        {
            "source": "recent_promotional_message",
            "text": "Flowers sale @seller_two https://two.example",
            "sender_id": 998822,
            "sender_role": "ordinary",
            "message_id": 789124,
            "age_hours": 26,
            "accessible": True,
            "warning_search_complete": True,
            "promotion_key": "second",
            "promotion_targets": ["https://two.example"],
            "warning_reply_ids": [],
        },
    ]


def test_redaction_preserves_indexes_roles_and_distinct_campaign_proof():
    original = evidence()
    safe = ai.anonymize_evidence(original)
    exported = json.dumps(safe)
    assert len(safe) == len(original) and trial_evidence(safe)
    for private in ["998811", "998822", "789123", "13812345678", "seller_one", "one.example"]:
        assert private not in exported
    assert original[1]["sender_id"] == 998811
    assert safe[1]["sender_role"] == "ordinary" and safe[1]["age_hours"] == 25


@pytest.mark.parametrize(
    "change",
    [
        "bio",
        "permission",
        "model",
        "edit",
        "retention",
        "last_risk_event_at",
        "spam_checked_at",
        "restriction_detected_at",
        "risk_pause_until",
    ],
)
def test_semantic_cache_changes_with_required_facts(change):
    original = evidence()
    snapshot = {
        "raw_peer_id": 42,
        "group_type": "supergroup",
        "permissions": {"can_send_text": True},
        "evidence": original,
    }
    account = Obj(id=2, profile_bio="old")
    limits = {"ad_policy_ai_model": "one", "ad_policy_ai_min_confidence": 95}
    before = ai.semantic_key(snapshot, ai.anonymize_evidence(original), account, limits)
    if change == "bio":
        account.profile_bio = "new"
    elif change == "permission":
        snapshot["permissions"]["can_send_text"] = False
    elif change == "model":
        limits["ad_policy_ai_model"] = "two"
    elif change == "edit":
        original[0].update(source="pinned_message", edited_at="2026-09-23")
    elif change == "retention":
        original[1]["age_hours"] = 23
    else:
        setattr(account, change, datetime.utcnow())
    assert (ai.semantic_key(snapshot, ai.anonymize_evidence(original), account, limits) != before) == (change in {"bio", "edit"})


@pytest.mark.asyncio
async def test_global_two_slots_and_old_holder_cannot_release_successor(test_db):
    now = datetime.utcnow()
    first = await ai.claim_slot(test_db, now=now)
    second = await ai.claim_slot(test_db, now=now)
    assert first and second and first[0] != second[0]
    assert await ai.claim_slot(test_db, now=now) is None
    newer = await ai.claim_slot(test_db, now=now + timedelta(seconds=301))
    assert newer and newer[0] == first[0]
    await ai.release_slot(test_db, first)
    row = await test_db.get(SystemSetting, newer[0], populate_existing=True)
    assert row.value == newer[1]
    await ai.release_slot(test_db, newer)
    assert await ai.claim_slot(test_db, now=now + timedelta(seconds=301)) is not None


@pytest.mark.asyncio
async def test_completed_semantics_never_expires_and_new_profile_is_new_material(test_db):
    account = Obj(id=2, profile_bio="old")
    snapshot = {
        "raw_peer_id": 42,
        "group_type": "supergroup",
        "permissions": {},
        "evidence": evidence(),
    }
    result = GroupAdRulesAuditResult(
        ad_allowed=True,
        policy_mode="soft_ad_allowed",
        confidence=99,
        ai_reviews=[{"confidence": 99}, {"confidence": 99}],
    )
    service = Obj(
        db=test_db,
        _ad_policy_llm=lambda: None,
        _evaluate_group_ad_rules_with_ai=AsyncMock(return_value=result),
    )
    limits = {"ad_policy_ai_enabled": True, "ad_policy_ai_model": "one"}
    await ai.review_semantics(service, snapshot, account, GroupAdRulesAuditResult(), limits)
    cached = await ai.review_semantics(
        service, snapshot, account, GroupAdRulesAuditResult(), limits
    )
    assert cached.cache_hit and service._evaluate_group_ad_rules_with_ai.await_count == 1
    row = await test_db.scalar(
        select(SystemSetting).where(SystemSetting.key.like("qualification.ai.once.%"))
    )
    data = json.loads(row.value)
    assert "expires_at" not in data
    data["expires_at"] = (datetime.utcnow() - timedelta(seconds=1)).isoformat()
    row.value = json.dumps(data)
    await test_db.commit()
    await ai.review_semantics(service, snapshot, account, GroupAdRulesAuditResult(), limits)
    assert service._evaluate_group_ad_rules_with_ai.await_count == 1
    account.profile_bio = "new"
    await ai.review_semantics(service, snapshot, account, GroupAdRulesAuditResult(), limits)
    assert service._evaluate_group_ad_rules_with_ai.await_count == 2


def test_waiting_ai_does_not_consume_group_errors_or_force_exit():
    now = datetime.utcnow()
    snapshot = {"decision": "observe", "reason": "group_rules_ai_queued", "ai_pending": True}
    old = {
        "technical_failures": 2,
        "ai_review_failures": 2,
        "observation_started_at": (now - timedelta(hours=60)).isoformat(),
    }
    decision, _, state, retry = qualification.review_schedule(snapshot, old, now)
    assert (decision, state) == ("observe", "waiting_ai") and retry > now
    assert snapshot["technical_failures"] == snapshot["ai_review_failures"] == 2


def test_unsupported_topic_does_not_turn_into_exit():
    now = datetime.utcnow()
    state = {
        "decision": "observe",
        "reason": "topic_route_requires_review",
        "quality_status": "qualified",
    }
    result = qualification.review_schedule(
        state, {"observation_started_at": (now - timedelta(hours=60)).isoformat()}, now
    )
    assert result[:3] == ("observe", "topic_route_requires_review", "completed")


@pytest.mark.asyncio
async def test_enqueue_membership_is_idempotent_and_caller_can_rollback(test_db):
    _, _, member, _ = await setup(test_db)
    row = await qualification.enqueue_membership_review(test_db, member, batch_id="join-attempt-11")
    row_id = row.id
    assert (
        await qualification.enqueue_membership_review(test_db, member, batch_id="join-attempt-11")
    ).id == row_id
    assert member.review_started_at is None and member.ad_status == "blocked"
    await test_db.rollback()
    assert await test_db.get(GroupQualificationAudit, row_id) is None


@pytest.mark.asyncio
async def test_fresh_switch_and_profile_changes_block_previously_authorized_send(test_db):
    account, group, _, _ = await setup(test_db)
    assert (
        await qualification.send_gate(test_db, 2, group.group_id, "Read my profile", None) is None
    )
    await test_db.execute(
        update(SystemSetting)
        .where(SystemSetting.key == qualification.SETTING_KEY)
        .values(value='{"enabled":false}')
    )
    assert (
        await qualification.send_gate(test_db, 2, group.group_id, "Read my profile", None)
        == "qualification_disabled"
    )
    await test_db.execute(
        update(SystemSetting)
        .where(SystemSetting.key == qualification.SETTING_KEY)
        .values(value='{"enabled":true,"rollout_accounts":{"2":{"phase":"dynamic"}}}')
    )
    account.profile_bio = "a new destination"
    await test_db.flush()
    assert (
        await qualification.send_gate(test_db, 2, group.group_id, "Read my profile", None)
        == "qualification_profile_changed"
    )


@pytest.mark.asyncio
async def test_verification_claim_prevents_second_owner(test_db):
    _, _, member, _ = await verification_setup(test_db)
    first = await verification._claim(test_db, member.id)
    assert first is not None and await verification._claim(test_db, member.id) is None
    token, ledger = first
    assert not await verification._save_claim(test_db, member.id, "wrong", ledger, release=True)
    assert await verification._save_claim(test_db, member.id, token, ledger, release=True)
    assert await verification._claim(test_db, member.id) is not None


@pytest.mark.asyncio
async def test_verification_switch_changed_during_account_lease_prevents_write(test_db):
    service, _, _, _ = await verification_setup(test_db)
    wrapper = service.account_pool.acquire_by_id.return_value

    async def acquired(*_args, **_kwargs):
        config = await test_db.get(SystemSetting, qualification.SETTING_KEY)
        config.value = '{"enabled":true,"execute_verification":false}'
        await test_db.commit()
        return wrapper

    service.account_pool.acquire_by_id.side_effect = acquired
    assert (await verification.run_verifications(service))["attempted"] == 0
    service.telegram_execution.click_verification_button.assert_not_awaited()
    service.account_pool.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_verification_preflight_not_sent_can_retry_same_challenge(test_db):
    service, _, member, _ = await verification_setup(test_db)
    service.telegram_execution.click_verification_button.side_effect = TelegramSendPreflightError(
        "budget busy", retry_after_seconds=10
    )
    assert (await verification.run_verifications(service))["attempted"] == 1
    row = await test_db.get(
        SystemSetting, f"qualification.verification.{member.id}", populate_existing=True
    )
    ledger = json.loads(row.value)
    assert ledger["actions"][0]["status"] == "not_sent"
    original = copy.deepcopy(ledger["actions"][0])
    ledger["next_check_at"] = (datetime.utcnow() - timedelta(seconds=1)).isoformat()
    row.value = json.dumps(ledger)
    await test_db.commit()
    service.telegram_execution.click_verification_button.side_effect = None
    assert (await verification.run_verifications(service))["attempted"] == 1
    row = await test_db.get(SystemSetting, row.key, populate_existing=True)
    actions = json.loads(row.value)["actions"]
    assert len(actions) == 1 and actions[0]["key"] == original["key"]
    assert actions[0]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_pilot_requires_matching_durable_reservation(test_db):
    from app.modules.acquisition.models import AdDeliveryLog

    _, group, member, row = await setup(test_db)
    row.evidence_hash = "a" * 64
    start = datetime.utcnow().isoformat()
    settings = await test_db.get(SystemSetting, qualification.SETTING_KEY)
    value = json.loads(settings.value)
    value["rollout_accounts"] = {"2": {"phase": "pilot", "started_at": start, "max_groups": 1}}
    settings.value = json.dumps(value)
    await test_db.commit()
    assert (
        await qualification.send_gate(test_db, 2, group.group_id, "Read profile", None)
        == "qualification_pilot_reservation_required"
    )
    context = {
        "audit_id": row.id,
        "evidence_hash": row.evidence_hash,
        "policy_version": POLICY_VERSION,
        "content_scope": "text_profile",
        "rollout_phase": "pilot",
        "rollout_started_at": start,
        "pilot_max_groups": 1,
        "account_id": 2,
        "group_type": "supergroup",
        "telegram_group_id": group.group_id,
        "membership_joined_at": member.joined_at.isoformat(),
    }
    delivery = AdDeliveryLog(
        account_id=2,
        group_id=group.id,
        telegram_group_id=group.group_id,
        ad_campaign_id=1,
        status="pending",
        reservation_token="pilot-one",
        qualification_context_json=json.dumps(context),
    )
    test_db.add(delivery)
    await test_db.commit()
    assert (
        await qualification.send_gate(
            test_db, 2, group.group_id, "Read profile", None, reservation_token="pilot-one"
        )
        is None
    )
    context["rollout_started_at"] = "old-pilot"
    delivery.qualification_context_json = json.dumps(context)
    await test_db.commit()
    assert (
        await qualification.send_gate(
            test_db, 2, group.group_id, "Read profile", None, reservation_token="pilot-one"
        )
        == "qualification_pilot_reservation_mismatch"
    )


@pytest.mark.asyncio
async def test_trial_repeat_requires_prior_survival_after_daily_window(test_db):
    from app.modules.acquisition.models import AdDeliveryLog

    _, group, _, row = await setup(test_db, decision="trial")
    test_db.add(
        AdDeliveryLog(
            account_id=2,
            group_id=group.id,
            telegram_group_id=group.group_id,
            ad_campaign_id=1,
            status="success",
            telegram_message_id=99,
            created_at=datetime.utcnow() - timedelta(days=2),
            qualification_context_json=json.dumps(
                {"decision": "trial", "group_type": "supergroup"}
            ),
        )
    )
    await test_db.commit()
    assert (
        await qualification.send_gate(test_db, 2, group.group_id, "Read profile", None)
        == "qualification_previous_survival_unresolved"
    )
    old = await test_db.scalar(select(AdDeliveryLog))
    old.survival_status = "survived"
    row.decision = "allowed"
    await test_db.commit()
    assert await qualification.send_gate(test_db, 2, group.group_id, "Read profile", None) is None


@pytest.mark.asyncio
async def test_model_wait_reuses_collection_without_occupying_telegram(monkeypatch, test_db):
    from app.modules.acquisition.group_qualification import EvidenceCollector
    from tests.unit.test_qualification_assess_rule_scope import assess

    original = EvidenceCollector.collect
    calls = []

    async def collect(self, *args, **kwargs):
        calls.append(True)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(EvidenceCollector, "collect", collect)
    _, row, llm = await assess(
        test_db, "允许普通成员文字广告", [TimeoutError(), TimeoutError()],
        assess_runs=2, ad_count=1, ad_age_hours=1
    )
    assert len(calls) == 1 and llm.generate.await_count == 1
    assert row.state == "completed" and row.decision == "reject"
    assert json.loads(row.evidence_json)["review_trigger"] == "ordinary_ad_maturity"


@pytest.mark.asyncio
async def test_due_survival_yields_review_claim_to_maintenance(monkeypatch, test_db):
    from app.modules.acquisition.models import AdDeliveryLog

    _, group, _, _ = await setup(test_db)
    await qualification.queue_reviews(test_db, [2], "priority-review")
    test_db.add(
        AdDeliveryLog(
            account_id=2,
            group_id=group.id,
            telegram_group_id=group.group_id,
            ad_campaign_id=1,
            status="success",
            survival_status="pending",
            survival_check_due_at=datetime.utcnow() - timedelta(seconds=1),
        )
    )
    await test_db.commit()
    collect = AsyncMock()
    monkeypatch.setattr(qualification, "assess", collect)
    runner = Obj(db=test_db, account_pool=Obj(acquire_by_id=AsyncMock()))
    assert (await qualification.run_reviews(runner, limit=2))["processed"] == 0
    collect.assert_not_awaited()


@pytest.mark.asyncio
async def test_unapproved_real_llm_destination_never_receives_evidence(test_db):
    from app.core.ai.llm_client import LLMClient

    llm = object.__new__(LLMClient)
    llm.base_url = "https://unapproved.example/v1"
    service = Obj(
        db=test_db, _ad_policy_llm=lambda: llm, _evaluate_group_ad_rules_with_ai=AsyncMock()
    )
    snapshot = {"evidence": evidence()}
    result = await ai.review_semantics(
        service,
        snapshot,
        Obj(id=2, profile_bio=""),
        GroupAdRulesAuditResult(),
        {"ad_policy_ai_enabled": True},
    )
    assert result.reason == "group_rules_ai_destination_unapproved"
    assert snapshot["ai_pending"] is False and snapshot["ai_decision"] == "fail"
    service._evaluate_group_ad_rules_with_ai.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_confirmed_audit_recovery_is_bounded_idempotent_and_preserves_history(
    test_db,
):
    _, _, member, row = await setup(test_db, decision="reject")
    row.policy_version = "pp-ai-qualification-v1"
    old_evidence = row.evidence_json
    await test_db.commit()
    config = {"enabled": True, "account_ids": [2]}
    ids = await qualification.ensure_membership_reviews(test_db, config)
    assert len(ids) == 1 and ids[0] != row.id
    current = await test_db.get(GroupQualificationAudit, ids[0])
    assert current.policy_version == POLICY_VERSION and current.state == "queued"
    assert current.membership_joined_at == member.joined_at
    assert member.ad_status == "blocked" and row.evidence_json == old_evidence
    assert await qualification.ensure_membership_reviews(test_db, config) == []


@pytest.mark.asyncio
async def test_all_account_exit_scope_queues_future_account_without_legacy_allowlist(test_db):
    _, _, member, row = await setup(test_db, decision="reject")
    row.policy_version = "pp-ai-qualification-v1"
    await test_db.commit()
    ids = await qualification.ensure_membership_reviews(
        test_db, {"enabled": True, "account_ids": [3], "exit_all_accounts": True}
    )
    assert len(ids) == 1
    current = await test_db.get(GroupQualificationAudit, ids[0])
    assert current.account_id == member.account_id
    assert current.policy_version == POLICY_VERSION


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hold", ["pending", "manual_required", "protected", "out_of_scope", "left"]
)
async def test_missing_audit_recovery_does_not_release_holds(test_db, hold):
    _, group, member, row = await setup(test_db)
    await test_db.delete(row)
    config = {"enabled": True, "account_ids": [2]}
    if hold in {"pending", "left"}:
        member.status = hold
        member.ad_status = "blocked"
    elif hold == "manual_required":
        member.review_status = hold
    elif hold == "protected":
        config["manual_protected_group_ids"] = [group.id]
    else:
        config["account_ids"] = [3]
    await test_db.commit()
    prior = (member.status, member.review_status, member.ad_status)
    recovered = await qualification.ensure_membership_reviews(test_db, config)
    if hold == "manual_required":
        assert len(recovered) == 1 and member.review_status == "initial_pending"
    elif hold == "protected":
        assert recovered == []
        assert (member.status, member.review_status, member.ad_status) == ("joined", "owned_group_excluded", "blocked")
    else:
        assert recovered == []
        assert (member.status, member.review_status, member.ad_status) == prior


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["v1", "different_namespace", "unknown_namespace"])
async def test_verification_requires_current_scope_and_real_namespace(test_db, change):
    service, client, _, row = await verification_setup(test_db)
    if change == "v1":
        row.policy_version = "pp-ai-qualification-v1"
    else:
        from telethon.tl.types import PeerChat

        client.get_entity = AsyncMock(
            return_value=PeerChat(1234567890)
            if change == "different_namespace"
            else Obj(id=1234567890)
        )
    await test_db.commit()
    assert (await verification.run_verifications(service))["attempted"] == 0
    service.telegram_execution.click_verification_button.assert_not_awaited()
    service.telegram_execution.send_verification_answer.assert_not_awaited()


def test_pending_ai_waits_longer_when_live_evidence_cannot_yet_pass():
    now = datetime.utcnow()
    previous = {"technical_failures": 1, "ai_review_failures": 2}
    for reason in (
        "ordinary_member_advertising_unverified",
        "no_other_online_member",
        "online_count_unknown",
        "system_account_identity_unconfirmed",
    ):
        check = {"decision": "observe", "reason": reason, "ai_pending": True}
        decision, actual_reason, state, retry = qualification.review_schedule(
            check, previous, now
        )
        assert (decision, actual_reason, state, retry) == (
            "observe", reason, "waiting_ai", now + timedelta(hours=2)
        )
        assert check["technical_failures"] == 1
        assert check["ai_review_failures"] == 2

    actionable = {
        "decision": "observe",
        "reason": "group_rules_ai_queued",
        "ai_pending": True,
    }
    assert qualification.review_schedule(actionable, previous, now) == (
        "observe", "group_rules_ai_queued", "waiting_ai", now + timedelta(minutes=5)
    )


@pytest.mark.asyncio
async def test_approved_ark_coding_destination_reaches_ai_queue(test_db, monkeypatch):
    from app.core.ai.llm_client import LLMClient

    llm = object.__new__(LLMClient)
    llm.base_url = "https://ark.cn-beijing.volces.com/api/coding/v3"
    service = Obj(
        db=test_db, _ad_policy_llm=lambda: llm, _evaluate_group_ad_rules_with_ai=AsyncMock()
    )
    monkeypatch.setattr(ai, "claim_slot", AsyncMock(return_value=None))
    snapshot = {"evidence": evidence()}
    result = await ai.review_semantics(
        service,
        snapshot,
        Obj(id=2, profile_bio=""),
        GroupAdRulesAuditResult(),
        {"ad_policy_ai_enabled": True, "ad_policy_ai_model": "glm-5.3-flash"},
    )
    assert result.reason == "group_rules_ai_capacity_unavailable"
    assert snapshot["ai_pending"] is False and snapshot["ai_decision"] == "fail"
    service._evaluate_group_ad_rules_with_ai.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrong_ark_coding_path_cannot_receive_group_evidence(test_db):
    from app.core.ai.llm_client import LLMClient

    llm = object.__new__(LLMClient)
    llm.base_url = "https://ark.cn-beijing.volces.com/api/coding/v3/v1"
    service = Obj(
        db=test_db, _ad_policy_llm=lambda: llm, _evaluate_group_ad_rules_with_ai=AsyncMock()
    )
    result = await ai.review_semantics(
        service,
        {"evidence": evidence()},
        Obj(id=2, profile_bio=""),
        GroupAdRulesAuditResult(),
        {"ad_policy_ai_enabled": True},
    )
    assert result.reason == "group_rules_ai_destination_unapproved"
    service._evaluate_group_ad_rules_with_ai.assert_not_awaited()
