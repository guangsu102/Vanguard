"""Own proven deliveries survive technical rechecks without Telegram reads."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.account.operation_lease import AccountOperationLeaseBusy
from app.core.account.telegram_execution import TelegramExecutionError
from app.modules.acquisition import qualification_service as service
from app.modules.acquisition.models import AdDeliveryLog, GroupQualificationAudit
from app.modules.acquisition.qualification_continuity import (
    OWN_SOURCE,
    choose_authorization,
    own_proof_valid,
    restore_own_authorization,
)
from tests.unit.test_qualification_actions import Client
from tests.unit.test_qualification_service import setup


def test_review_fallback_cannot_revive_an_old_exit_or_skip_manual_required():
    queued = Obj(state="queued", decision="unknown", reason=None)
    rejection = Obj(state="completed", decision="reject", reason="old_exit")
    assert choose_authorization([queued, rejection]) is queued
    manual = Obj(state="manual_required", decision="technical_wait", reason="telegram_read_budget")
    approval = Obj(state="completed", decision="trial", reason=None)
    assert choose_authorization([manual, approval]) is manual


async def proven(db, reason="telegram_read_budget"):
    account, group, member, row = await setup(db, decision="trial")
    sent = datetime.utcnow() - timedelta(days=2)
    observed = sent + timedelta(hours=24, minutes=2)
    snapshot = json.loads(row.evidence_json)
    snapshot.update(
        collected_at=(sent - timedelta(minutes=10)).isoformat(),
        checked_at=(sent - timedelta(minutes=9)).isoformat(),
        permissions={"member": True, "can_send_text": True},
        advertising_audit={
            "ad_allowed": True,
            "policy_mode": "soft_ad_trial",
            "decision_source": "verified_ordinary_member_precedent",
        },
    )
    row.checked_at = sent - timedelta(minutes=9)
    row.evidence_json = json.dumps(snapshot)
    context = {
        k: snapshot[k]
        for k in (
            "account_id",
            "telegram_group_id",
            "group_type",
            "policy_version",
            "content_scope",
            "decision",
        )
    }
    context.update(
        audit_id=row.id,
        evidence_hash="original",
        membership_joined_at=member.joined_at.isoformat(),
        survival_observations=[
            {
                "stage": "twenty_four_hour",
                "checked_at": observed.isoformat(),
                "reason": None,
                "facts": dict.fromkeys(
                    (
                        "exists",
                        "is_own_message",
                        "member",
                        "can_send",
                        "group_accessible",
                        "account_readable",
                    ),
                    True,
                )
                | {"account_user_id": 200, "errors": []},
            }
        ],
    )
    log = AdDeliveryLog(
        account_id=account.id,
        group_id=group.id,
        telegram_group_id=group.group_id,
        ad_campaign_id=1,
        status="success",
        telegram_message_id=987,
        sent_at=sent,
        survival_status="survived",
        survival_stage="complete",
        survived_twenty_four_hour_at=observed,
        qualification_context_json=json.dumps(context),
    )
    db.add(log)
    if reason:
        row.decision = (
            "technical_wait"
            if reason in {"telegram_read_budget", "AccountOperationLeaseBusy"}
            else "observe"
        )
        row.reason = reason
        row.checked_at = datetime.utcnow()
        row.evidence_json = json.dumps(
            {**snapshot, "decision": row.decision, "reason": reason, "previous_checks": [snapshot]}
        )
        member.review_status, member.ad_status = "review_2h", "warming"
    await db.commit()
    return account, group, member, row, log


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        "telegram_read_budget",
        "AccountOperationLeaseBusy",
        "advertising_precedent_disappeared_before_send",
        "advertising_precedent_identity_changed_before_send",
        "group_rules_ai_consensus_failed",
        "group_rules_unavailable",
    ],
)
async def test_recovers_each_historical_cohort_without_network(test_db, reason):
    account, group, member, old, log = await proven(test_db, reason)
    old_json = old.evidence_json
    preview = await restore_own_authorization(test_db, member.id)
    assert preview["eligible"] and member.review_status != "approved"
    recovered = await restore_own_authorization(test_db, member.id, apply=True)
    assert recovered["restored"]
    await test_db.commit()
    assert old.evidence_json == old_json and old.decision not in {"allowed", "trial"}
    row, _, _ = await service.current_authorization(test_db, account.id, group.group_id)
    snapshot = json.loads(row.evidence_json)
    assert snapshot["authorization_basis"] == OWN_SOURCE
    assert row.checked_at == log.survived_twenty_four_hour_at
    assert row.expires_at == row.checked_at + timedelta(hours=720)
    assert await own_proof_valid(test_db, snapshot, member, group)
    assert await service.send_gate(test_db, account.id, group.group_id, "查看简介", None) is None
    repeated = await restore_own_authorization(test_db, member.id, apply=True)
    assert repeated["reason"] == "already_established"
    assert len((await test_db.scalars(select(GroupQualificationAudit))).all()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "rejoined",
        "profile",
        "wrong_peer",
        "wrong_account_user",
        "missing_observation",
        "early_check",
        "negative",
        "manual_pause",
    ],
)
async def test_recovery_rejects_unproven_or_changed_scope(test_db, change):
    account, group, member, row, log = await proven(test_db)
    context = json.loads(log.qualification_context_json)
    if change == "rejoined":
        member.joined_at = datetime.utcnow()
    if change == "profile":
        account.profile_bio = "changed profile"
    if change == "wrong_peer":
        context["group_type"] = "basic_group"
        log.telegram_group_id = -group.group_id
    if change == "wrong_account_user":
        context["survival_observations"][0]["facts"]["account_user_id"] = 300
    if change == "missing_observation":
        context["survival_observations"] = []
    if change == "early_check":
        log.survived_twenty_four_hour_at = log.sent_at + timedelta(hours=1)
    if change == "negative":
        row.decision = "reject"
        row.reason = "verified_permission_denied"
    if change == "manual_pause":
        member.ad_status = "paused"
    log.qualification_context_json = json.dumps(context)
    await test_db.commit()
    result = await restore_own_authorization(test_db, member.id, apply=True)
    assert not result["restored"] and not result.get("eligible")


@pytest.mark.asyncio
async def test_live_guard_uses_local_own_evidence_but_keeps_current_permissions(test_db):
    from app.modules.acquisition.qualification_actions import refresh_live_authorization

    account, group, member, _, _ = await proven(test_db)
    await restore_own_authorization(test_db, member.id, apply=True)
    await test_db.commit()
    client = Client()
    client.entity.id = group.group_id
    client.get_messages = AsyncMock(side_effect=AssertionError("must not reread third party"))
    await refresh_live_authorization(test_db, client, account.id, group.group_id)
    client.get_messages.assert_not_awaited()
    client.permissions.has_left = True
    with pytest.raises(TelegramExecutionError, match="current_permission_changed"):
        await refresh_live_authorization(test_db, client, account.id, group.group_id)


@pytest.mark.asyncio
async def test_new_review_does_not_cancel_valid_authorization_but_rejection_does(test_db):
    account, group, member, row = await setup(test_db)
    ids = await service.queue_reviews(test_db, [account.id], "recheck")
    await test_db.commit()
    assert member.review_status == "approved" and member.ad_status == "active"
    assert await service.send_gate(test_db, account.id, group.group_id, "查看简介", None) is None
    pending = await test_db.get(GroupQualificationAudit, ids[0])
    pending.state = "completed"
    pending.decision = "reject"
    pending.reason = "new_rejection"
    await test_db.commit()
    resolved, _, _ = await service.current_authorization(test_db, account.id, group.group_id)
    assert resolved.id == pending.id
    assert (
        await service.send_gate(test_db, account.id, group.group_id, "查看简介", None) is not None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        AccountOperationLeaseBusy("busy"),
        TimeoutError(),
        ConnectionError(),
    ],
)
async def test_transient_renewal_does_not_revoke_approval(test_db, monkeypatch, exc):
    from app.modules.acquisition.qualification_renewal import try_light_renewal
    from tests.unit.test_qualification_light_renewal import fixture

    service_, account, group, member, row, previous = await fixture(test_db)
    from app.modules.acquisition.qualification_events import request
    await request(test_db, member, "gap")
    await test_db.flush()
    service_.account_pool.acquire_by_id.side_effect = exc
    expiry, evidence = row.expires_at, row.evidence_json
    result = await try_light_renewal(
        service_, account, group, member, row, previous, force_refresh=False
    )
    assert not result.passed and row.decision == "trial"
    assert row.expires_at == expiry and row.evidence_json == evidence


@pytest.mark.asyncio
async def test_established_recheck_needs_no_account_lease_or_read_budget(test_db):
    from app.modules.acquisition.qualification_renewal import try_light_renewal

    account, group, member, _, _ = await proven(test_db)
    await restore_own_authorization(test_db, member.id, apply=True)
    await test_db.commit()
    row, _, _ = await service.current_authorization(test_db, account.id, group.group_id)
    expiry = row.expires_at
    pool = Obj(add_account_from_db=AsyncMock(side_effect=AssertionError("no Telegram reads")))
    result = await try_light_renewal(
        Obj(db=test_db, account_pool=pool),
        account,
        group,
        member,
        row,
        json.loads(row.evidence_json),
        force_refresh=False,
    )
    assert result.passed and row.expires_at == expiry
    pool.add_account_from_db.assert_not_awaited()
    row.expires_at = datetime.utcnow() - timedelta(seconds=1)
    result = await try_light_renewal(
        Obj(db=test_db, account_pool=pool),
        account,
        group,
        member,
        row,
        json.loads(row.evidence_json),
        force_refresh=False,
    )
    assert result.passed  # Event grants do not expire merely with the calendar.
    pool.add_account_from_db.assert_not_awaited()
