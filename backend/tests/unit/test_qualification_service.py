"""Durable qualification lifecycle and scoped side-effect authorization."""

import hashlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.account.models import AccountStatus, TelegramAccount
from app.core.group.models import Group, GroupAccountMembership
from app.core.settings_models import SystemSetting
from app.modules.acquisition import qualification_service as service
from app.modules.acquisition.group_qualification import POLICY_VERSION
from app.modules.acquisition.models import AdDeliveryLog, GroupQualificationAudit


@pytest.fixture(autouse=True)
def available_read_budget(monkeypatch):
    monkeypatch.setattr("app.core.account.read_schedule.check_read_ready", AsyncMock())


async def setup(db, *, decision="allowed", account_id=2):
    now = datetime.utcnow()
    account = TelegramAccount(
        id=account_id,
        identifier=f"qualification-test-{account_id}",
        session_name=f"qualification-test-{account_id}",
        status=AccountStatus.ONLINE,
        is_active=True,
        risk_level="normal",
    )
    group = Group(id=40, group_id=1234567890, username="Qualification_Group", title="test")
    member = GroupAccountMembership(
        id=60,
        group_id=40,
        telegram_group_id=group.group_id,
        account_id=account_id,
        status="joined",
        joined_at=now - timedelta(days=5),
        review_status="approved",
        ad_status="active",
    )
    row = GroupQualificationAudit(
        batch_id="baseline",
        membership_id=60,
        account_id=account_id,
        group_id=40,
        policy_version=POLICY_VERSION,
        content_scope="text_profile",
        state="completed",
        decision=decision,
        membership_joined_at=member.joined_at,
        checked_at=now,
        expires_at=now + timedelta(hours=24),
        evidence_json=json.dumps(
            {
                "decision": decision,
                "quality_status": "qualified",
                "evidence": [],
                "account_id": account_id,
                "group_id": group.id,
                "telegram_group_id": group.group_id,
                "group_type": "supergroup",
                "policy_version": POLICY_VERSION,
                "content_scope": "text_profile",
                "profile_fingerprint": hashlib.sha256(b"").hexdigest(),
            }
        ),
    )
    db.add_all(
        [
            account,
            group,
            member,
            row,
            SystemSetting(key="automation.ad_delivery_execution", value='{"enabled":true}'),
            SystemSetting(
                key=service.SETTING_KEY,
                value=json.dumps(
                    {
                        "enabled": True,
                        "rollout_accounts": {"2": {"phase": "dynamic"}, "3": {"phase": "dynamic"}},
                        "execute_exits": True,
                        "account_ids": [2, 3, 4],
                        "promotion_account_ids": [2, 3],
                        "system_user_ids": [200, 300, 400],
                        "system_account_user_ids": {"2": 200},
                    }
                ),
            ),
        ]
    )
    await db.commit()
    from app.modules.acquisition.qualification_system_identity import covered_system_ids

    _, fingerprint = await covered_system_ids(db, await service.policy(db), account_id=account_id)
    proof_date = now - timedelta(hours=30)
    prior = json.loads(row.evidence_json)
    prior["system_identity_coverage"] = True
    prior["system_identity_fingerprint"] = fingerprint
    prior["system_user_ids"] = [200, 300, 400]
    prior["evidence"] = [
        {
            "source": "recent_promotional_message",
            "message_id": 101 + index,
            "sender_id": 501 + index,
            "sender_role": "ordinary",
            "text": f"优惠出售服务联系 https://seller-{index}.example",
            "date": proof_date.isoformat(),
            "edited_at": None,
            "age_hours": 30,
            "accessible": True,
            "warning_search_complete": True,
            "warning_reply_ids": [],
        }
        for index in range(2)
    ]
    row.evidence_json = json.dumps(prior)
    await db.commit()
    return account, group, member, row


def snapshot(decision="observe", **kwargs):
    return {
        "decision": decision,
        "reason": "advertising_evidence_insufficient",
        "quality_status": "qualified",
        **kwargs,
    }


def test_observation_begins_with_valid_evidence_not_queue_age():
    now = datetime.utcnow()
    first = snapshot()
    verdict, _, state, retry = service.review_schedule(first, {}, now)
    assert (verdict, state) == ("observe", "completed")
    assert retry == now + timedelta(hours=2)
    assert first["observation_started_at"] == now.isoformat()


def test_second_review_waits_for_24_hour_final_review():
    now = datetime.utcnow()
    original = {"observation_started_at": (now - timedelta(hours=2)).isoformat()}
    _, _, _, retry = service.review_schedule(snapshot(), original, now)
    assert retry == now + timedelta(hours=22)


def test_final_complete_evidence_insufficiency_is_rejected():
    now = datetime.utcnow()
    original = {"observation_started_at": (now - timedelta(hours=24)).isoformat()}
    verdict, reason, state, retry = service.review_schedule(snapshot(), original, now)
    assert (verdict, state, retry) == ("reject", "completed", None)
    assert reason == "advertising_evidence_insufficient_after_observation"


@pytest.mark.parametrize(
    "incomplete",
    [
        {"unknowns": ["history_hidden"]},
        {"rules_incomplete": True},
        {"quality_status": "observe"},
    ],
)
def test_unknown_facts_at_deadline_require_handling_not_exit(incomplete):
    now = datetime.utcnow()
    verdict, _, state, retry = service.review_schedule(
        snapshot(**incomplete),
        {
            "observation_started_at": (now - timedelta(hours=25)).isoformat(),
        },
        now,
    )
    assert (verdict, state, retry) == ("observe", "completed", None)


def test_three_technical_failures_do_not_start_observation_or_authorize_exit():
    now = datetime.utcnow()
    result = snapshot("technical_wait", technical_errors=["TimeoutError"])
    verdict, _, state, retry = service.review_schedule(result, {"technical_failures": 2}, now)
    assert (verdict, state, retry) == ("technical_wait", "completed", now + timedelta(hours=2))
    assert result["observation_started_at"] is None


def test_account_lease_busy_retries_soon_without_spending_read_failure_limit():
    now = datetime.utcnow()
    check = snapshot(
        "technical_wait",
        reason="AccountOperationLeaseBusy",
        technical_errors=["AccountOperationLeaseBusy"],
    )
    verdict, reason, state, retry = service.review_schedule(check, {}, now)
    assert (verdict, reason, state, retry) == (
        "technical_wait", "AccountOperationLeaseBusy", "completed", now + timedelta(minutes=2)
    )
    assert check["technical_failures"] == 0
    assert check["observation_started_at"] is None
    assert check["lease_wait_started_at"] == now.isoformat()
    later = snapshot("technical_wait", reason="AccountOperationLeaseBusy")
    _, _, state, retry = service.review_schedule(
        later, {"technical_failures": 2, "lease_wait_started_at": now.isoformat()},
        now + timedelta(hours=2),
    )
    assert (state, retry) == ("completed", now + timedelta(hours=2, minutes=2))
    assert later["technical_failures"] == 2
    expired = snapshot("technical_wait", reason="AccountOperationLeaseBusy")
    _, _, state, retry = service.review_schedule(
        expired, {"lease_wait_started_at": now.isoformat()}, now + timedelta(hours=24)
    )
    assert (state, retry) == ("completed", now + timedelta(hours=26))
    timeout = snapshot("technical_wait", reason="TimeoutError")
    assert service.review_schedule(timeout, {}, now)[3] == now + timedelta(hours=2)

def test_known_wait_can_extend_to_48_hours_but_never_beyond():
    now = datetime.utcnow()
    original = {"observation_started_at": (now - timedelta(hours=24)).isoformat()}
    pending = snapshot(
        "wait", permissions={"temporary_until": (now + timedelta(days=4)).isoformat()}
    )
    _, _, state, retry = service.review_schedule(pending, original, now)
    assert state == "completed"
    assert retry == now + timedelta(hours=24)
    _, _, state, retry = service.review_schedule(pending, original, now + timedelta(hours=24))
    assert (state, retry) == ("completed", now + timedelta(hours=26))


@pytest.mark.asyncio
async def test_requeue_preserves_valid_approval_and_join_version(test_db):
    _, _, member, _ = await setup(test_db)
    joined = member.joined_at
    ids = await service.queue_reviews(test_db, [2], "new-batch")
    assert len(ids) == 1
    assert member.joined_at == joined
    assert member.review_started_at is None and member.review_deadline_at is None
    assert (
        await service.send_gate(test_db, 2, "@qualification_group", "欢迎查看我的简介", None)
        is None
    )


@pytest.mark.asyncio
async def test_batch_idempotency_preserves_requested_scope_even_for_empty_accounts(test_db):
    await setup(test_db)
    test_db.add(
        TelegramAccount(
            id=3,
            identifier="empty-account",
            session_name="empty-account",
            status=AccountStatus.ONLINE,
        )
    )
    await test_db.commit()
    ids = await service.queue_reviews(test_db, [2, 3], "full-request")
    assert await service.queue_reviews(test_db, [3, 2], "full-request") == ids
    with pytest.raises(ValueError, match="scope_mismatch"):
        await service.queue_reviews(test_db, [2], "full-request")
    assert await service.queue_reviews(test_db, [3], "empty-batch") == []
    assert await service.queue_reviews(test_db, [3], "empty-batch") == []
    with pytest.raises(ValueError, match="scope_mismatch"):
        await service.queue_reviews(test_db, [2, 3], "empty-batch")


@pytest.mark.asyncio
async def test_username_and_numeric_target_resolve_same_account_scoped_authorization(test_db):
    _, group, _, row = await setup(test_db)
    assert (
        await service.send_gate(test_db, 2, "@qualification_group", "请查看简介中的交流入口", None)
        is None
    )
    resolved, found, _ = await service.current_authorization(
        test_db, 2, "https://t.me/Qualification_Group"
    )
    assert resolved.id == row.id and found.id == group.id
    assert (
        await service.send_gate(test_db, 3, group.group_id, "请查看简介", None)
        == "qualification_membership_missing"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,reason",
    [
        ("expired", "qualification_expired"),
        ("changed_membership", "qualification_review_required"),
        ("new_queue", None),
        ("restricted", "qualification_account_unavailable"),
        ("media", "qualification_content_scope_changed"),
        ("url", "qualification_content_scope_changed"),
    ],
)
async def test_send_gate_rechecks_scope_account_and_version(test_db, change, reason):
    account, group, member, row = await setup(test_db)
    text, media = "请查看简介", None
    if change == "expired":
        row.expires_at = datetime.utcnow() - timedelta(seconds=1)
    elif change == "changed_membership":
        member.joined_at = datetime.utcnow()
    elif change == "new_queue":
        test_db.add(
            GroupQualificationAudit(
                batch_id="new",
                membership_id=member.id,
                account_id=2,
                group_id=group.id,
                policy_version=POLICY_VERSION,
                state="queued",
            )
        )
    elif change == "restricted":
        account.status = AccountStatus.RESTRICTED
    elif change == "media":
        media = "https://example.test/image.jpg"
    elif change == "url":
        text = "交流地址 https://example.test"
    await test_db.flush()
    assert await service.send_gate(test_db, 2, group.group_id, text, media) == reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,other_account,reason",
    [
        ("success", 2, "qualification_group_daily_cap"),
        ("success", 3, "qualification_group_daily_cap"),
        ("sending", 3, "qualification_group_delivery_in_flight"),
        ("send_unknown", 2, "qualification_delivery_reconciliation_required"),
    ],
)
async def test_cross_account_daily_cap_and_unknown_delivery(test_db, status, other_account, reason):
    _, group, _, _ = await setup(test_db)
    test_db.add(
        AdDeliveryLog(
            account_id=other_account,
            group_id=group.id,
            telegram_group_id=group.group_id,
            ad_campaign_id=1,
            status=status,
        )
    )
    await test_db.flush()
    assert await service.send_gate(test_db, 2, group.group_id, "请查看简介", None) == reason


@pytest.mark.asyncio
async def test_current_reservation_token_ignores_only_its_own_row(test_db):
    _, group, _, _ = await setup(test_db)
    test_db.add(
        AdDeliveryLog(
            account_id=2,
            group_id=group.id,
            telegram_group_id=group.group_id,
            ad_campaign_id=1,
            status="sending",
            reservation_token="this-send",
        )
    )
    await test_db.flush()
    assert (
        await service.send_gate(
            test_db, 2, group.group_id, "请查看简介", None, reservation_token="this-send"
        )
        is None
    )
    assert (
        await service.send_gate(
            test_db, 2, group.group_id, "请查看简介", None, reservation_token="other-send"
        )
        == "qualification_group_delivery_in_flight"
    )


@pytest.mark.asyncio
async def test_other_account_fresh_group_rule_ban_cannot_be_evaded(test_db):
    _, group, member, _ = await setup(test_db)
    test_db.add(
        GroupQualificationAudit(
            batch_id="group-ban",
            membership_id=999,
            account_id=3,
            group_id=group.id,
            policy_version=POLICY_VERSION,
            state="completed",
            decision="reject",
            checked_at=datetime.utcnow(),
            evidence_json=json.dumps({"group_level_advertising_ban": True, "group_type": "supergroup"}),
        )
    )
    await test_db.flush()
    assert (
        await service.send_gate(test_db, 2, group.group_id, "请查看简介", None)
        == "qualification_group_rule_prohibits"
    )


@pytest.mark.asyncio
async def test_verified_ordinary_precedent_overrides_historical_group_ad_ban(test_db):
    _, group, _, row = await setup(test_db, decision="trial")
    snapshot = json.loads(row.evidence_json)
    snapshot["advertising_audit"] = {
        "decision_source": "verified_ordinary_member_precedent"
    }
    snapshot["precedent_overrides_group_ad_ban"] = True
    snapshot["evidence"].append(
        {"source": "full_about", "text": "本群禁止任何广告和推广。"}
    )
    row.evidence_json = json.dumps(snapshot)
    test_db.add(
        GroupQualificationAudit(
            batch_id="earlier-ad-ban",
            membership_id=999,
            account_id=3,
            group_id=group.id,
            policy_version=POLICY_VERSION,
            state="completed",
            decision="reject",
            checked_at=datetime.utcnow() - timedelta(days=1),
            evidence_json=json.dumps(
                {
                    "group_level_advertising_ban": True,
                    "group_type": "supergroup",
                    "evidence": [
                        {"source": "full_about", "text": "本群禁止任何广告和推广。"}
                    ],
                }
            ),
        )
    )
    await test_db.flush()
    assert await service.send_gate(test_db, 2, group.group_id, "请查看简介", None) is None
    # A current verified precedent also supersedes other historical rule bans.
    test_db.add(
        GroupQualificationAudit(
            batch_id="different-earlier-ban",
            membership_id=998,
            account_id=3,
            group_id=group.id,
            policy_version=POLICY_VERSION,
            state="completed",
            decision="reject",
            checked_at=datetime.utcnow() - timedelta(days=2),
            evidence_json=json.dumps(
                {
                    "group_level_advertising_ban": True,
                    "group_type": "supergroup",
                    "evidence": [{"source": "full_about", "text": "禁止发送外链。"}],
                }
            ),
        )
    )
    await test_db.flush()
    assert (
        await service.send_gate(test_db, 2, group.group_id, "请查看简介", None)
        is None
    )


@pytest.mark.asyncio
async def test_exit_requires_fresh_reject_pending_state_and_no_manual_protection(test_db):
    account, group, member, row = await setup(test_db, decision="reject")
    member.review_status = "exit_pending"
    proof = json.loads(row.evidence_json)
    proof.update({
        "coverage": [{"complete": True}, {"complete": True}],
        "ordinary_advertising_last_48h_count": 0,
        "collected_at": datetime.utcnow().isoformat(),
        "permissions": {"member": True},
        "rules_incomplete": False,
        "exit_group_rule_ban": True,
        "technical_errors": [],
        "unknowns": [],
        "group_level_advertising_ban": True,
        "advertising_audit": {"ad_allowed": False, "policy_mode": "forbidden"},
        "member_count_verified": True,
        "member_count": 49,
        "online_count": 1,
    })
    row.evidence_json = json.dumps(proof)
    await test_db.flush()
    assert (await service.authorize_leave(test_db, 2, group, member))[0]
    row.decision = "technical_wait"
    await test_db.flush()
    assert not (await service.authorize_leave(test_db, 2, group, member))[0]
    row.decision = "reject"
    config = await test_db.get(SystemSetting, service.SETTING_KEY)
    settings = json.loads(config.value)
    settings["manual_protected_group_ids"] = [group.id]
    config.value = json.dumps(settings)
    await test_db.flush()
    assert await service.authorize_leave(test_db, 2, group, member) == (
        False,
        "qualification_protected",
    )


@pytest.mark.asyncio
async def test_queue_retries_due_technical_evidence_three_times_without_exit(test_db):
    account, group, member, _ = await setup(test_db)
    account.status = AccountStatus.RESTRICTED
    ids = await service.queue_reviews(test_db, [2], "technical-batch")
    fake_service = SimpleNamespace(
        db=test_db,
        _join_review_account_block_reason=lambda *_: None,
        _sync_group_ad_policy_from_audit=AsyncMock(),
    )
    await test_db.commit()
    for _attempt in range(3):
        row = await test_db.get(GroupQualificationAudit, ids[0])
        row.next_retry_at = datetime.utcnow() - timedelta(seconds=1)
        await test_db.commit()
        outcome = await service.run_reviews(fake_service)
        assert outcome["processed"] == 1
        assert row.decision == "technical_wait"
    assert row.state == "completed"
    assert member.review_status == "approved"
    assert member.review_started_at is None
    assert len(json.loads(row.evidence_json)["previous_checks"]) == 2
    assert not (await service.authorize_leave(test_db, 2, group, member))[0]


@pytest.mark.asyncio
async def test_queue_does_not_execute_future_reviews_or_changed_memberships(test_db, monkeypatch):
    _, _, member, _ = await setup(test_db)
    ids = await service.queue_reviews(test_db, [2], "future-batch")
    row = await test_db.get(GroupQualificationAudit, ids[0])
    row.next_retry_at = datetime.utcnow() + timedelta(hours=2)
    await test_db.commit()
    fake_service = SimpleNamespace(db=test_db)
    assessed = []

    async def assess_current(actor, account_id, group, *, row):
        assert row.membership_joined_at == member.joined_at
        assessed.append(row.id)
        row.state, row.decision, row.next_retry_at = "completed", "observe", None
        return SimpleNamespace(passed=False, reason="checked_current_membership")

    monkeypatch.setattr(service, "assess", assess_current)
    assert (await service.run_reviews(fake_service))["processed"] == 0
    assert assessed == []
    row.next_retry_at = datetime.utcnow() - timedelta(seconds=1)
    member.joined_at = datetime.utcnow()
    await test_db.commit()
    # Cancelling the obsolete scope no longer consumes the execution quantum;
    # missing-membership recovery creates a new row for the current join.
    assert (await service.run_reviews(fake_service))["processed"] == 1
    assert row.state == "cancelled"
    assert len(assessed) == 1 and assessed[0] != row.id


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["allowed", "observe"])
async def test_expired_review_read_wait_releases_account_for_other_due_work(test_db, monkeypatch, decision):
    _, _, member, row = await setup(test_db, decision=decision)
    now = datetime.utcnow()
    row.state = "running"
    row.next_retry_at = now - timedelta(minutes=1)
    row.expires_at = now - timedelta(minutes=1)
    original_evidence = row.evidence_json
    other_group = Group(id=41, group_id=1234567891, title="second")
    other_member = GroupAccountMembership(id=61, group_id=41, telegram_group_id=other_group.group_id,
        account_id=2, status="joined", joined_at=member.joined_at, review_status="pending")
    other = GroupQualificationAudit(batch_id="second", membership_id=61, account_id=2, group_id=41,
        policy_version=POLICY_VERSION, content_scope="text_profile", state="queued",
        decision="observe", membership_joined_at=member.joined_at, next_retry_at=now)
    test_db.add_all([other_group, other_member, other])
    await test_db.commit()
    monkeypatch.setattr(service, "ensure_membership_reviews", AsyncMock())
    monkeypatch.setattr(service, "ai_evidence_reusable", lambda *args: False)
    wait_until = now + timedelta(minutes=35)
    monkeypatch.setattr("app.core.account.read_schedule.read_wait",
                        AsyncMock(return_value=("telegram_read_budget", wait_until)))
    assess = AsyncMock()
    monkeypatch.setattr(service, "assess", assess)
    await service.run_reviews(SimpleNamespace(db=test_db), account_id=2, limit=2)
    await test_db.refresh(row)
    await test_db.refresh(other)
    assert row.state == ("completed" if decision == "allowed" else "queued")
    assert row.evidence_json == original_evidence and row.decision == decision
    assert row.next_retry_at == wait_until
    assert other.next_retry_at == wait_until  # The second row was not excluded by a false claim.
    assert row.attempts == other.attempts == 0
    assess.assert_not_awaited()


@pytest.mark.asyncio
async def test_queue_never_collects_same_account_while_existing_review_running(test_db):
    _, group, _, _ = await setup(test_db)
    ids = await service.queue_reviews(test_db, [2], "waiting-batch")
    test_db.add(
        GroupQualificationAudit(
            batch_id="running-other",
            membership_id=999,
            account_id=2,
            group_id=group.id,
            policy_version=POLICY_VERSION,
            state="running",
            next_retry_at=datetime.utcnow() + timedelta(minutes=10),
        )
    )
    await test_db.commit()
    assert (await service.run_reviews(SimpleNamespace(db=test_db)))["processed"] == 0
    assert (await test_db.get(GroupQualificationAudit, ids[0])).state == "queued"


@pytest.mark.asyncio
async def test_unidentified_own_reservation_cannot_authorize_a_retry(test_db):
    _, group, _, _ = await setup(test_db)
    test_db.add(
        AdDeliveryLog(
            account_id=2,
            group_id=group.id,
            telegram_group_id=group.group_id,
            ad_campaign_id=1,
            status="sending",
            reservation_token="unreconciled",
        )
    )
    await test_db.flush()
    assert (
        await service.send_gate(test_db, 2, group.group_id, "请查看简介", None)
        == "qualification_group_delivery_in_flight"
    )


@pytest.mark.asyncio
async def test_unknown_delivery_older_than_24_hours_still_requires_reconciliation(test_db):
    _, group, _, _ = await setup(test_db)
    test_db.add(
        AdDeliveryLog(
            account_id=2,
            group_id=group.id,
            telegram_group_id=group.group_id,
            ad_campaign_id=1,
            status="send_unknown",
            created_at=datetime.utcnow() - timedelta(days=2),
        )
    )
    await test_db.flush()
    assert (
        await service.send_gate(test_db, 2, group.group_id, "请查看简介", None)
        == "qualification_delivery_reconciliation_required"
    )


@pytest.mark.asyncio
async def test_changed_canonical_group_identity_invalidates_send_and_exit(test_db):
    _, group, member, row = await setup(test_db)
    snapshot = json.loads(row.evidence_json)
    snapshot["telegram_group_id"] = 111222333
    row.evidence_json = json.dumps(snapshot)
    await test_db.flush()
    assert (
        await service.send_gate(test_db, 2, group.group_id, "请查看简介", None)
        == "qualification_scope_changed"
    )
    row.decision = "reject"
    member.review_status = "exit_pending"
    await test_db.flush()
    assert await service.authorize_leave(test_db, 2, group, member) == (
        False,
        "qualification_exit_scope_changed",
    )


@pytest.mark.asyncio
async def test_rule_only_allowed_group_passes_send_gate_without_ordinary_ad(test_db):
    _, group, _, row = await setup(test_db, decision="allowed")
    data = json.loads(row.evidence_json)
    data["evidence"] = [{"source": "full_about", "text": "允许普通成员发布文字广告"}]
    row.evidence_json = json.dumps(data)
    await test_db.commit()
    assert await service.send_gate(test_db, 2, group.group_id, "请查看简介", None) is None
    row.decision = "trial"
    await test_db.commit()
    assert (
        await service.send_gate(test_db, 2, group.group_id, "请查看简介", None)
        == "qualification_ad_precedent_unconfirmed"
    )


def test_approval_has_no_calendar_only_recheck():
    now = datetime.utcnow()
    verdict, _, state, retry = service.review_schedule(snapshot("trial"), {}, now)
    assert (verdict, state) == ("trial", "completed")
    assert retry is None


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["trial", "allowed"])
async def test_older_group_ban_blocks_newer_account_review(test_db, decision):
    _, group, _, row = await setup(test_db, decision=decision)
    test_db.add(
        GroupQualificationAudit(
            batch_id="earlier-ban",
            membership_id=999,
            account_id=3,
            group_id=group.id,
            policy_version=POLICY_VERSION,
            state="completed",
            decision="reject",
            checked_at=row.checked_at - timedelta(days=3),
            evidence_json=json.dumps({"group_level_advertising_ban": True, "group_type": "supergroup"}),
        )
    )
    await test_db.flush()
    assert (
        await service.send_gate(test_db, 2, group.group_id, "请查看简介", None)
        == "qualification_group_rule_prohibits"
    )


@pytest.mark.asyncio
async def test_overwritten_ban_evidence_still_blocks_account_switch(test_db):
    _, group, _, _ = await setup(test_db, decision="trial")
    test_db.add(
        GroupQualificationAudit(
            batch_id="overwritten-ban",
            membership_id=999,
            account_id=3,
            group_id=group.id,
            policy_version=POLICY_VERSION,
            state="completed",
            decision="observe",
            checked_at=datetime.utcnow() - timedelta(days=2),
            evidence_json=json.dumps(
                {
                    "previous_checks": [{"group_level_advertising_ban": True, "group_type": "supergroup"}],
                }
            ),
        )
    )
    await test_db.flush()
    assert (
        await service.send_gate(test_db, 2, group.group_id, "请查看简介", None)
        == "qualification_group_rule_prohibits"
    )


def test_ban_fact_is_independent_of_bounded_previous_checks():
    initial = {
        "group_level_advertising_ban": True,
        "checked_at": datetime.utcnow().isoformat(),
        "evidence": [{"source": "pinned_message", "text": "禁止广告"}],
    }
    facts = service.confirmed_group_bans(initial)
    later = {"confirmed_group_bans": facts, "previous_checks": [{"decision": "observe"}] * 6}
    assert service.confirmed_group_bans(later) == facts
    assert facts[0]["rule_fingerprint"]


@pytest.mark.asyncio
async def test_manual_clearance_requires_changed_rules_and_explicit_permission(test_db):
    _, group, _, row = await setup(test_db)
    now = datetime.utcnow()
    ban = {
        "rule_fingerprint": service.authoritative_rule_fingerprint(
            {
                "evidence": [{"source": "pinned_message", "text": "禁止广告"}],
            }
        ),
        "confirmed_at": (now - timedelta(days=2)).isoformat(),
    }
    approved = json.loads(row.evidence_json)
    approved.update(
        {
            "reason": "explicit_permission_verified",
            "evidence": [{"source": "pinned_message", "text": "现允许普通成员发布文字广告"}],
        }
    )
    row.evidence_json, row.evidence_hash = json.dumps(approved), "new-evidence"
    clearance = {
        "group_id": group.id,
        "ban_rule_fingerprint": ban["rule_fingerprint"],
        "review_audit_id": row.id,
        "review_evidence_hash": row.evidence_hash,
        "operator_id": 1,
        "reviewed_at": now.isoformat(),
    }
    config = {"group_ban_clearances": [clearance]}
    assert service.group_ban_cleared(config, group, row, ban)
    assert service.group_ban_cleared({}, group, row, ban)
    row.decision = "trial"
    assert not service.group_ban_cleared(config, group, row, ban)
    row.decision = "allowed"
    approved["evidence"] = [{"source": "pinned_message", "text": "禁止广告"}]
    row.evidence_json = json.dumps(approved)
    assert not service.group_ban_cleared(config, group, row, ban)


@pytest.mark.parametrize("seconds", [7201, 86400, 200000])
def test_flood_wait_is_not_retried_before_telegram_deadline(seconds):
    now = datetime.utcnow()
    check = snapshot(
        "technical_wait", technical_errors=["FloodWaitError"], retry_after_seconds=seconds
    )
    _, _, state, retry = service.review_schedule(check, {}, now)
    assert state == "completed"
    assert retry >= now + timedelta(seconds=seconds)
    _, _, state, retry = service.review_schedule(check, {"technical_failures": 2}, now)
    assert (state, retry) == ("completed", now + timedelta(seconds=max(7200, seconds)))


def test_verified_permanent_send_restriction_rejects_without_waiting_forever():
    now = datetime.utcnow()
    check = snapshot(
        quality_status="observe",
        permissions={
            "member": True,
            "can_send_text": False,
            "permanent_send_restriction_verified": True,
        },
    )
    decision, reason, state, retry = service.review_schedule(check, {}, now)
    assert (decision, state, retry) == ("reject", "completed", None)
    assert reason == "account_permanent_send_restriction"


@pytest.mark.parametrize(
    "extra",
    [
        {"verification_required": True},
        {"verification_pending": True},
        {"temporary_until": "2027-01-01T00:00:00"},
        {"newcomer_until": "2027-01-01T00:00:00"},
        {"slowmode_until": "2027-01-01T00:00:00"},
        {"permanent_send_restriction_verified": False},
    ],
)
def test_verification_wait_or_unconfirmed_permission_is_not_permanent_rejection(extra):
    check = snapshot(
        quality_status="observe",
        permissions={
            "member": True,
            "can_send_text": False,
            "permanent_send_restriction_verified": True,
            **extra,
        },
    )
    assert service.review_schedule(check, {}, datetime.utcnow())[0] == "observe"


@pytest.mark.parametrize(
    "risk_level,paused,enabled,expected",
    [
        ("normal", False, True, True),
        ("watch", False, True, True),
        ("frozen", False, True, False),
        ("quarantined", False, True, False),
        ("normal", True, True, False),
        ("normal", False, False, False),
    ],
)
def test_restricted_read_permission_has_narrow_configuration_boundary(
    risk_level, paused, enabled, expected
):
    now = datetime.utcnow()
    account = SimpleNamespace(
        status=AccountStatus.RESTRICTED,
        is_active=True,
        risk_level=risk_level,
        risk_pause_until=now + timedelta(hours=1) if paused else None,
    )
    assert (
        service.restricted_evidence_read_allowed(
            account, {"allow_restricted_evidence_reads": enabled}, now
        )
        is expected
    )


@pytest.mark.asyncio
async def test_restricted_read_collects_facts_without_approving_leaving_or_changing_status(
    test_db, monkeypatch
):
    account, group, member, row = await setup(test_db)
    account.status = AccountStatus.RESTRICTED
    setting = await test_db.get(SystemSetting, service.SETTING_KEY)
    config = json.loads(setting.value)
    config["allow_restricted_evidence_reads"] = True
    setting.value = json.dumps(config)
    await test_db.commit()
    client = SimpleNamespace(
        get_entity=AsyncMock(return_value=SimpleNamespace(id=group.group_id)),
        get_me=AsyncMock(return_value=SimpleNamespace(id=200)),
        send_message=AsyncMock(),
        send_file=AsyncMock(),
    )
    pool = SimpleNamespace(
        add_account_from_db=AsyncMock(),
        acquire_by_id=AsyncMock(return_value=SimpleNamespace(client=client)),
        release=AsyncMock(),
    )
    collector = SimpleNamespace(
        collect=AsyncMock(
            return_value={
                "quality_status": "reject",
                "quality_reason": "members_below_50",
                "member_count": 49,
                "valid_messages": 8,
                "permissions": {"member": True, "can_send_text": True},
            }
        )
    )
    monkeypatch.setattr(service, "EvidenceCollector", lambda *args, **kwargs: collector)
    fake = SimpleNamespace(
        db=test_db,
        account_pool=pool,
        _join_review_account_block_reason=lambda *_: "account_status_unavailable",
        _telegram_entity_matches_group_id=lambda *_: True,
    )
    for _ in range(3):
        audit = await service.assess(fake, account.id, group, row=row)
        assert not audit.should_leave and not audit.passed and audit.ad_allowed is None
    payload = json.loads(row.evidence_json)
    assert payload["member_count"] == 49 and payload["valid_messages"] == 8
    assert payload["evidence_decision"] == "reject"
    assert payload["account_eligibility"]["can_promote"] is False
    assert row.state == "completed"
    assert account.status == AccountStatus.RESTRICTED
    assert all(
        call.kwargs["allow_restricted"] is True for call in pool.acquire_by_id.call_args_list
    )
    client.send_message.assert_not_called()
    client.send_file.assert_not_called()


def test_rejection_history_deduplication_preserves_confirmed_exit_and_full_facts():
    entry = {
        "audit_id": 10,
        "checked_at": "2026-09-22T00:00:00",
        "evidence_hash": "facts",
        "reason": "members_below_50",
        "exit_confirmed_at": None,
        "snapshot": {"member_count": 49, "valid_messages": 8},
    }
    confirmed = {**entry, "exit_confirmed_at": "2026-09-22T01:00:00"}
    assert service.merge_exit_history([entry, confirmed, entry]) == [confirmed]


@pytest.mark.asyncio
async def test_recheck_preserves_exit_history_across_rows_and_only_appends_real_reject(
    test_db, monkeypatch
):
    account, group, member, prior = await setup(test_db, decision="reject")
    old = json.loads(prior.evidence_json)
    old.update({"reason": "members_below_50", "member_count": 49, "valid_messages": 8})
    fact = service.qualification_exit_fact(prior, old)
    fact["exit_confirmed_at"] = datetime.utcnow().isoformat()
    old["qualification_exit_history"] = [fact]
    prior.evidence_json = json.dumps(old)
    await test_db.commit()
    queued = await service.queue_reviews(test_db, [2], "history-batch")
    row = await test_db.get(GroupQualificationAudit, queued[0])
    client = SimpleNamespace(
        get_entity=AsyncMock(return_value=SimpleNamespace(id=group.group_id)),
        get_me=AsyncMock(return_value=SimpleNamespace(id=200)),
    )
    pool = SimpleNamespace(
        add_account_from_db=AsyncMock(),
        acquire_by_id=AsyncMock(return_value=SimpleNamespace(client=client)),
        release=AsyncMock(),
    )
    collected = {
        "quality_status": "reject",
        "quality_reason": "members_below_50",
        "member_count": 48,
        "valid_messages": 8,
        "permissions": {"member": True, "can_send_text": True},
    }
    monkeypatch.setattr(
        service,
        "EvidenceCollector",
        lambda *args, **kwargs: SimpleNamespace(collect=AsyncMock(return_value=dict(collected))),
    )
    fake = SimpleNamespace(
        db=test_db,
        account_pool=pool,
        _join_review_account_block_reason=lambda *_: None,
        _telegram_entity_matches_group_id=lambda *_: True,
    )
    await service.assess(fake, 2, group, row=row)
    payload = json.loads(row.evidence_json)
    assert row.decision == "reject"
    assert any(
        item["exit_confirmed_at"] == fact["exit_confirmed_at"]
        for item in payload["qualification_exit_history"]
    )
    current = [
        item for item in payload["qualification_exit_history"] if item["audit_id"] == row.id
    ][0]
    assert current["snapshot"]["member_count"] == 48 and current["snapshot"]["valid_messages"] == 8
    assert "qualification_exit_history" not in current["snapshot"]
    assert "previous_checks" not in current["snapshot"]
    before = len(payload["qualification_exit_history"])
    collected.update(
        {
            "quality_status": "technical_wait",
            "quality_reason": "network_failed",
            "technical_errors": ["TimeoutError"],
        }
    )
    await service.assess(fake, 2, group, row=row)
    assert row.decision == "technical_wait"
    assert len(json.loads(row.evidence_json)["qualification_exit_history"]) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("current_entry_exists", [True, False])
async def test_confirmed_exit_binds_exact_mutable_review_and_flushes_left_state(
    test_db, current_entry_exists
):
    from app.modules.acquisition.qualification_actions import record_confirmed_exit

    _, group, member, row = await setup(test_db, decision="reject")
    row.reason = "members_below_50"
    row.evidence_hash = "current-review-evidence"
    current_checked_at = row.checked_at
    old_checked_at = current_checked_at - timedelta(hours=3)
    data = json.loads(row.evidence_json)
    data.update(reason=row.reason, checked_at=current_checked_at.isoformat(), member_count=20)
    current = service.qualification_exit_fact(row, data)
    old = {**current, "checked_at": old_checked_at.isoformat(), "evidence_hash": "old-evidence"}
    wrong_hash = {**current, "evidence_hash": "different-evidence-at-same-time"}
    wrong_time = {**current, "checked_at": old_checked_at.isoformat()}
    history = [old, wrong_hash, wrong_time]
    if current_entry_exists:
        history.append(current)
    data["qualification_exit_history"] = history
    row.evidence_json = json.dumps(data)
    await test_db.commit()

    confirmed_at = datetime.utcnow()
    member.status = member.review_status = "left"
    member.ad_status = "blocked"
    member.left_at = member.leave_confirmed_at = confirmed_at
    # Without the helper's explicit flush, populate_existing inside its lookup
    # would restore the previous joined/active values when autoflush is disabled.
    with test_db.no_autoflush:
        await record_confirmed_exit(test_db, member, group)
    assert member.status == "left" and member.review_status == "left"
    assert member.leave_confirmed_at == confirmed_at
    await test_db.commit()
    await test_db.refresh(member)
    await test_db.refresh(row)
    saved = json.loads(row.evidence_json)["qualification_exit_history"]
    assert len(saved) == 4
    assert all(item["exit_confirmed_at"] is None for item in saved[:3])
    assert saved[3]["exit_confirmed_at"] == confirmed_at.isoformat()
    assert saved[3]["checked_at"] == current_checked_at.isoformat()
    assert saved[3]["evidence_hash"] == row.evidence_hash
    assert saved[3]["snapshot"]["member_count"] == 20
    assert member.status == "left" and member.ad_status == "blocked"

    await record_confirmed_exit(test_db, member, group)
    await test_db.commit()
    await test_db.refresh(row)
    assert json.loads(row.evidence_json)["qualification_exit_history"] == saved


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_legacy_refresh_stays_disabled_while_dedicated_queue_is_paused_or_active(test_db, enabled):
    from app.modules.acquisition.automation import AcquisitionAutomationService

    test_db.add(SystemSetting(key=service.SETTING_KEY, value=json.dumps({
        "enabled": enabled, "legacy_ad_policy_refresh_enabled": False, "account_ids": [2,3,4],
    })))
    await test_db.commit()
    runner = AcquisitionAutomationService(test_db)
    runner._sync_account_pool = AsyncMock()
    result = await runner.refresh_group_ad_policies()
    assert result["processed"] == 0
    assert result["reason"] == "dedicated_qualification_queue"
    runner._sync_account_pool.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_execution_pause_does_not_claim_or_acquire_telegram(monkeypatch):
    setting = SystemSetting(
        key=service.SETTING_KEY,
        value=json.dumps({"enabled": True, "execute_reviews": False, "execute_exits": True}),
    )
    db = SimpleNamespace(get=AsyncMock(return_value=setting), scalar=AsyncMock(),
                         commit=AsyncMock(), flush=AsyncMock())
    actor = SimpleNamespace(db=db, account_pool=SimpleNamespace(acquire=AsyncMock()))
    assess = AsyncMock()
    monkeypatch.setattr(service, "assess", assess)
    assert await service.run_reviews(actor, limit=5) == {
        "processed": 0, "results": [], "paused": True, "reason": "qualification_reviews_paused",
    }
    db.get.assert_awaited_once_with(SystemSetting, service.SETTING_KEY, populate_existing=True)
    db.scalar.assert_not_awaited()
    db.commit.assert_not_awaited()
    db.flush.assert_not_awaited()
    actor.account_pool.acquire.assert_not_awaited()
    assess.assert_not_awaited()


@pytest.mark.asyncio
async def test_paused_reviews_preserve_due_queue_and_exit_authorization(test_db):
    _, group, member, row = await setup(test_db, decision="reject")
    member.review_status = "exit_pending"
    config = await test_db.get(SystemSetting, service.SETTING_KEY)
    settings = json.loads(config.value)
    settings["execute_reviews"] = False
    config.value = json.dumps(settings)
    row.next_retry_at = datetime.utcnow() - timedelta(seconds=1)
    await test_db.commit()
    before = (row.state, row.attempts, row.next_retry_at, member.review_status)
    assert (await service.run_reviews(SimpleNamespace(db=test_db)))["paused"] is True
    await test_db.refresh(row)
    await test_db.refresh(member)
    assert (row.state, row.attempts, row.next_retry_at, member.review_status) == before
    assert not (await service.authorize_leave(test_db, 2, group, member))[0]


@pytest.mark.asyncio
async def test_confirmed_nonmember_reconciles_without_group_rejection_or_exit(test_db, monkeypatch):
    _, group, member, _ = await setup(test_db)
    ids = await service.queue_reviews(test_db, [2], "confirmed-nonmember-batch")

    async def confirmed_absence(actor, account_id, target, *, row):
        assert account_id == 2 and target.id == group.id
        row.decision = "technical_wait"
        row.reason = "membership_not_participant"
        row.state = "completed"
        row.checked_at = datetime.utcnow()
        row.next_retry_at = row.checked_at + timedelta(hours=2)
        row.evidence_json = json.dumps({
            "decision": row.decision,
            "reason": row.reason,
            "permissions": {
                "member": False,
                "membership_evidence": "telegram_permissions_not_participant",
            },
        })
        await actor.db.flush()
        return SimpleNamespace()

    monkeypatch.setattr(service, "assess", confirmed_absence)
    actor = SimpleNamespace(db=test_db, _sync_group_ad_policy_from_audit=AsyncMock())
    outcome = await service.run_reviews(actor)
    assert outcome["processed"] == 1
    row = await test_db.get(GroupQualificationAudit, ids[0])
    await test_db.refresh(member)
    assert (row.decision, row.reason, row.state, row.next_retry_at) == (
        "technical_wait", "membership_not_participant", "completed", None,
    )
    assert (member.status, member.review_status, member.ad_status) == (
        "left", "left", "blocked",
    )
    assert member.left_at == row.checked_at
    assert member.leave_requested_at is None
    actor._sync_group_ad_policy_from_audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_unmapped_managed_account_invalidates_prior_send_approval(test_db):
    _, group, _, _ = await setup(test_db)
    test_db.add(
        TelegramAccount(
            id=3, identifier="new-unmapped-promoter", session_name="new-unmapped-promoter",
            status=AccountStatus.OFFLINE,
        )
    )
    await test_db.commit()
    assert (
        await service.send_gate(test_db, 2, group.group_id, "请查看简介", None)
        == "qualification_system_identity_unconfirmed"
    )


@pytest.mark.asyncio
async def test_legacy_system_id_list_alone_never_proves_coverage(test_db):
    _, group, _, _ = await setup(test_db)
    setting = await test_db.get(SystemSetting, service.SETTING_KEY)
    config = json.loads(setting.value)
    del config["system_account_user_ids"]
    setting.value = json.dumps(config)
    await test_db.commit()
    assert (
        await service.send_gate(test_db, 2, group.group_id, "请查看简介", None)
        == "qualification_system_identity_unconfirmed"
    )


@pytest.mark.asyncio
async def test_live_self_identity_must_match_registered_account_mapping(test_db):
    await setup(test_db)
    from app.modules.acquisition.qualification_system_identity import covered_system_ids

    config = await service.policy(test_db)
    _, fingerprint = await covered_system_ids(test_db, config, account_id=2, live_user_id=201)
    assert fingerprint is None


async def test_budget_wait_preserves_evidence_and_attempts(test_db, monkeypatch):
    from app.core.account.rpc_governor import RpcDeferred
    _, _, member, old = await setup(test_db)
    ids = await service.queue_reviews(test_db, [2], "budget-wait")
    row = await test_db.get(GroupQualificationAudit, ids[0])
    row.next_retry_at = datetime.utcnow() - timedelta(seconds=1)
    row.checked_at = datetime.utcnow() - timedelta(hours=1)
    await test_db.commit()
    previous = (row.attempts, row.checked_at, row.evidence_json, old.next_retry_at)
    monkeypatch.setattr("app.core.account.read_schedule.check_read_ready",
        AsyncMock(side_effect=RpcDeferred("telegram_read_budget", 600)))
    collect = AsyncMock()
    monkeypatch.setattr(service, "assess", collect)
    start = datetime.utcnow()
    result = await service.run_reviews(SimpleNamespace(db=test_db), limit=3)
    assert result["processed"] == 0
    assert (row.attempts, row.checked_at, row.evidence_json, old.next_retry_at) == previous
    assert row.next_retry_at >= start + timedelta(seconds=600)
    collect.assert_not_awaited()


async def test_qualification_reserve_wait_prevents_claim(test_db, monkeypatch):
    from app.core.account import rpc_governor as rpc
    _, _, member, old = await setup(test_db)
    ids = await service.queue_reviews(test_db, [2], "sync-bootstrap-wait")
    row = await test_db.get(GroupQualificationAudit, ids[0])
    row.next_retry_at = datetime.utcnow() - timedelta(seconds=1)
    row.checked_at = datetime.utcnow() - timedelta(hours=1)
    await test_db.commit()
    previous = (row.attempts, row.checked_at, row.evidence_json, old.next_retry_at)
    monkeypatch.setattr("app.core.account.read_schedule.check_read_ready", rpc.check_read_ready)
    monkeypatch.setattr(rpc, "snapshot", AsyncMock(return_value={
        "state": "recovering", "reason": None, "retry_after_seconds": 0,
        "lanes": {
            "routine": {"remaining": 0, "retry_after_seconds": 2400},
            "critical": {"remaining": 0, "retry_after_seconds": 2400},
            "sync": {"remaining": 0, "retry_after_seconds": 2400},
        },
    }))
    collect = AsyncMock()
    monkeypatch.setattr(service, "assess", collect)
    start = datetime.utcnow()
    result = await service.run_reviews(SimpleNamespace(db=test_db), limit=3)
    assert result["processed"] == 0
    assert (row.attempts, row.checked_at, row.evidence_json, old.next_retry_at) == previous
    assert row.next_retry_at >= start + timedelta(seconds=2400)
    collect.assert_not_awaited()
