from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.account import rpc_governor as rpc
from app.core.account.critical_fairness import purpose_budget
from app.modules.acquisition.ad_pacing import pacing_deadline
from app.modules.acquisition.callback_evidence import hint_ids, save_hint, trim
from app.modules.acquisition.execution_status import business_status, summary_status
from tests.unit.test_ad_read_reserve import mock_usage

NOW = datetime(2026, 9, 29, 12)


@pytest.mark.asyncio
@pytest.mark.parametrize("dominant,other", [("review", "join"), ("join", "review")])
async def test_busy_workload_cannot_take_other_workloads_last_hour_slice(test_db, monkeypatch, dominant, other):
    mock_usage(monkeypatch, {"hour": (57, 1800), "join_reserved_hour": (57, 1800),
                             f"critical_{dominant}_hour": (57, 1800)})
    # Join demand lending may lend a proven-idle join quarter to review (that
    # behavior is pinned by the join demand lending tests). This test pins the
    # static last-hour-slice guarantee of the workload budgets themselves.
    async def unforecasted(*args, **kwargs):
        return None
    monkeypatch.setattr("app.core.account.join_demand_lending.add_join_demand_headroom", unforecasted)
    state = await rpc.snapshot(test_db, 2, NOW)
    assert state["critical_workloads"][dominant]["remaining"] == 0
    assert state["critical_workloads"][other]["remaining"] == 15
    assert state["critical_workloads"][dominant]["retry_after_seconds"] == 1800
    assert state["lanes"]["ad"]["remaining"] >= 84  # protected share plus still-idle flex
    assert state["usage"]["hour"]["used"] == 57


@pytest.mark.asyncio
async def test_old_exhausted_parent_and_telegram_cooldown_still_block(test_db, monkeypatch):
    mock_usage(monkeypatch, {"day": (2400, 1200)})
    for purpose in ("auto_join", "group_qualification", "join_candidate_preview"):
        with pytest.raises(rpc.RpcDeferred):
            await rpc.check_read_ready(test_db, 2, NOW, purpose=purpose)
    mock_usage(monkeypatch, {})
    monkeypatch.setattr(rpc, "load_state", AsyncMock(return_value={"pause_until": (NOW + timedelta(hours=1)).isoformat()}))
    with pytest.raises(rpc.RpcDeferred, match="telegram_rpc_cooldown"):
        await rpc.check_read_ready(test_db, 2, NOW, purpose="auto_join")


def test_join_status_uses_join_purpose_and_does_not_wait_for_routine_pool():
    state = {"state": "ready", "lanes": {"routine": {"retry_after_seconds": 900}},
             "critical_workloads": {"join": {"remaining": 15, "retry_after_seconds": 0},
                                     "review": {"remaining": 0, "retry_after_seconds": 1800}}}
    assert purpose_budget(state, "auto_join")["remaining"] == 15
    status = business_status(state, now=NOW, ads_enabled=True, join_enabled=True,
                             executable_ad=0, executable_join=1, ad_due=None, join_due=None, join_blocker=None)
    assert status["join"]["state"] == "ready"
    assert status["review"]["state"] == "budget_wait"
    assert summary_status(status, quarantined=False, risk_reason=None)["state"] == "scheduled"


def test_no_scheduled_success_appearance_when_both_businesses_budget_blocked():
    until = (NOW + timedelta(hours=1)).isoformat()
    state = {"state": "ready", "lanes": {"ad": {"retry_after_seconds": 3600, "resume_at": until},
                                           "critical": {"retry_after_seconds": 3600, "resume_at": until}}}
    status = business_status(state, now=NOW, ads_enabled=True, join_enabled=True,
                             executable_ad=0, executable_join=0, ad_due=None, join_due=None, join_blocker=None)
    summary = summary_status(status, quarantined=False, risk_reason=None)
    assert summary == {"state": "budget_wait", "reason": "telegram_read_budget", "resume_at": until}


def test_business_wait_keeps_longer_lane_deadline_than_global_cooldown():
    from app.modules.acquisition.execution_status import read_status
    global_end, lane_end = (NOW + timedelta(minutes=2)).isoformat(), (NOW + timedelta(hours=1)).isoformat()
    state = {"state": "cooldown", "reason": "telegram_rpc_cooldown", "resume_at": global_end,
             "lanes": {"ad": {"remaining": 0, "retry_after_seconds": 3600, "resume_at": lane_end}}}
    assert read_status(state, "ad_delivery")["resume_at"] == lane_end


def budget(used, ttl):
    return {"limits": {"ad_day": 600}, "usage": {"day": {"ttl_seconds": ttl}, "ad_day": {"used": used}}}


def test_ads_spread_over_remaining_window_without_moving_reset_deadline():
    last = NOW - timedelta(minutes=10)
    due = pacing_deadline(budget(0, 86400), capacity=40, delivery_cost=12, last_sent=last, now=NOW)
    assert due == last + timedelta(minutes=36)
    # Repeated polling doesn't restart the pause once the retry reserve is reached.
    due = pacing_deadline(budget(480, 3600), capacity=40, delivery_cost=12, last_sent=last, now=NOW)
    later = pacing_deadline(budget(480, 3000), capacity=40, delivery_cost=12, last_sent=last, now=NOW + timedelta(minutes=10))
    assert due == later == NOW + timedelta(seconds=3601)
    assert pacing_deadline(budget(0, 86400), capacity=40, delivery_cost=12, last_sent=None, now=NOW) is None


@pytest.mark.asyncio
async def test_callback_ids_expire_and_never_cross_membership_epochs(test_db):
    member = SimpleNamespace(id=20, account_id=2, telegram_group_id=-1001234, joined_at=NOW - timedelta(days=2))
    fact = {"joined_at": member.joined_at.isoformat(), "message_id": 77, "message_date": (NOW - timedelta(hours=25)).isoformat()}
    await save_hint(test_db, member, fact, NOW)
    await test_db.commit()
    assert await hint_ids(test_db, member, NOW) == [77]
    assert await hint_ids(test_db, member, NOW + timedelta(hours=48)) == []
    member.joined_at = NOW
    assert await hint_ids(test_db, member, NOW) == []


@pytest.mark.asyncio
async def test_broken_optional_callback_cache_does_not_block_a_review(test_db):
    from app.core.settings_models import SystemSetting
    from app.modules.acquisition.callback_evidence import key
    member = SimpleNamespace(id=21, account_id=2, telegram_group_id=-1001234, joined_at=NOW)
    test_db.add(SystemSetting(key=key(member), value="broken-json"))
    await test_db.commit()
    assert await hint_ids(test_db, member, NOW) == []
    await save_hint(test_db, member, {"joined_at": NOW.isoformat(), "message_id": 77, "message_date": NOW.isoformat()}, NOW)
    await test_db.commit()
    assert await hint_ids(test_db, member, NOW) == [77]


def test_callback_cache_bounds_both_mature_and_new_candidates():
    items = [{"id": i + 1, "date": (NOW - timedelta(hours=i / 5)).isoformat()} for i in range(1000)]
    kept = trim(items, NOW)
    assert len(kept) == 80
    assert sum(datetime.fromisoformat(item["date"]) <= NOW - timedelta(hours=24) for item in kept) == 40


@pytest.mark.asyncio
async def test_callback_hint_is_fetched_live_before_used_as_evidence():
    from app.modules.acquisition.group_qualification import EvidenceCollector
    client = SimpleNamespace(get_messages=AsyncMock(return_value=[]))
    collector = EvidenceCollector(client)
    collector.callback_message_ids = [77]
    result = {"technical_errors": []}
    assert await collector._resume_messages("group", result, NOW) == []
    client.get_messages.assert_awaited_once_with("group", ids=[77])
    assert "evidence" not in result


@pytest.mark.asyncio
async def test_callback_projection_is_durable_bounded_and_never_archives_text(tmp_path):
    import json

    from app.core.account.listener_interest import project
    from tests.unit.test_listener_journal import client, event
    from tests.unit.test_selective_listener import PEER, policy
    c = client(tmp_path)
    watched = policy(groups={PEER: {"evidence_needed": True, "joined_at": NOW.isoformat()}})
    c.session.journal.policy = lambda: watched
    for index in range(1, 101):
        value = event(index)
        value.updates[0].message.message = "购买优惠 https://example.com/promo"
        envelope, _, facts = project(value, watched)
        assert len(facts) == 1 and facts[0]["kind"] == "evidence_candidate"
        assert "message" not in facts[0] and envelope.updates[0].message.message == ""
        c.session.journal.put_nowait(value)
    rows = c.session.journal.facts()
    assert len(rows) == 32
    assert all("example.com" not in value for _, value in rows)
    assert len({json.loads(value)["message_id"] for _, value in rows}) == 32
    c.session.close()
