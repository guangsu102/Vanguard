"""A real first-cycle DB transition must also enable the next mature send."""
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.account.models import AccountOperationConfig
from app.modules.acquisition.adaptive_frequency import DAY, FrequencyService
from app.modules.acquisition.daily_frequency import DailyFrequencyService
from app.modules.acquisition.models import GroupQualificationAudit
from app.modules.acquisition.qualification_actions import validate_live_send
from app.modules.acquisition.qualification_continuity import OWN_SOURCE
from app.modules.acquisition.qualification_service import current_authorization, send_gate
from tests.unit.test_daily_group_frequency import facts
from tests.unit.test_qualification_continuity import proven

pytestmark = pytest.mark.asyncio


async def first_cycle(db):
    account, group, member, audit, log = await proven(db, reason=None)
    db.add(AccountOperationConfig(account_id=account.id, enabled=True, auto_ads_enabled=True,
        dynamic_capacity_enabled=True, adaptive_ads_enabled=True))
    context = json.loads(log.qualification_context_json)
    context["survival_observations"] = []
    frequency = FrequencyService(db)
    state = await frequency.state(group.group_id, context, create=True, lock=True, now=log.sent_at)
    context["frequency"] = frequency.context(state)
    log.qualification_context_json = json.dumps(context)
    log.survival_status = "pending"
    log.survived_twenty_four_hour_at = None
    log.survived_two_minute_at = log.survived_one_hour_at = None
    daily = DailyFrequencyService(db)
    await daily.note_sent(log)
    await db.commit()
    return account, group, member, audit, log, frequency, state, daily


async def finish(db, log, state, daily):
    at = log.sent_at + DAY
    claim = await daily.claim(state.telegram_group_id, at)
    await db.commit()
    observed = {**facts(log), "account_user_id": 200}
    assert await daily.finish(claim, observed, None, at) == "daily_survived"
    await db.commit()
    return claim, observed, at


async def test_24h_success_establishes_own_grant_and_mature_lane_atomically(test_db):
    account, group, member, old, log, frequency, state, daily = await first_cycle(test_db)
    old_evidence = old.evidence_json
    assert await daily.claim(state.telegram_group_id, log.sent_at + DAY - timedelta(seconds=1)) is None
    claim, observed, at = await finish(test_db, log, state, daily)
    assert state.mature and state.quota == 2
    assert frequency.context(state)["lane"] == "mature"
    row, _, _ = await current_authorization(test_db, account.id, group.group_id)
    snapshot = json.loads(row.evidence_json)
    assert row.id != old.id and old.evidence_json == old_evidence
    assert snapshot["authorization_basis"] == OWN_SOURCE
    assert snapshot["authorization_mode"] == "events"
    assert member.last_ad_survived_at == at and row.next_retry_at is None
    assert await send_gate(test_db, account.id, group.group_id, "查看简介", None) is None
    await validate_live_send(test_db, object(), account.id, group.group_id)
    assert await daily.finish(claim, observed, None, at) == "daily_stale_claim"
    assert len((await test_db.scalars(select(GroupQualificationAudit))).all()) == 2


@pytest.mark.parametrize("change", ["paused", "rejoined", "rejected", "pending_event"])
async def test_survival_cannot_revive_a_protected_or_changed_membership(test_db, change):
    account, group, member, old, log, frequency, state, daily = await first_cycle(test_db)
    if change == "paused":
        member.ad_status = "paused"
    elif change == "rejoined":
        member.joined_at = datetime.utcnow()
    elif change == "rejected":
        old.decision, old.reason = "reject", "verified_permission_denied"
    else:
        from app.modules.acquisition.qualification_events import request
        await request(test_db, member, "membership")
    await test_db.commit()
    await finish(test_db, log, state, daily)
    assert len((await test_db.scalars(select(GroupQualificationAudit))).all()) == 1
    assert member.last_ad_survived_at is None
    assert await send_gate(test_db, account.id, group.group_id, "查看简介", None) is not None


async def test_24h_read_unknown_never_grants_maturity_or_own_authorization(test_db):
    account, group, member, old, log, frequency, state, daily = await first_cycle(test_db)
    at = log.sent_at + DAY
    claim = await daily.claim(state.telegram_group_id, at)
    await test_db.commit()
    assert await daily.finish(claim, {}, "telegram_read_budget", at) == "daily_review_retry"
    await test_db.commit()
    assert state.quota == 1 and not state.mature
    assert log.survived_twenty_four_hour_at is None
    assert len((await test_db.scalars(select(GroupQualificationAudit))).all()) == 1
