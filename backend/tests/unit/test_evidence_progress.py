from datetime import timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
from telethon.errors import ChatAdminRequiredError

from app.core.group.models import Group
from app.core.settings_models import SystemSetting
from app.modules.acquisition import candidate_inventory as inventory
from app.modules.acquisition import qualification_service as service
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.candidate_preview import CandidatePreview
from app.modules.acquisition.evidence_progress import evidence_maturity
from app.modules.acquisition.group_qualification import EvidenceCollector, trial_evidence
from tests.unit.test_group_qualification_sampling import ENTITY, NOW, ReadOnlyClient, message

AD = "特价服务出售欢迎联系 https://offer.example"


def evidence(age=23):
    return {
        "source": "recent_promotional_message",
        "sender_role": "ordinary",
        "sender_id": 42,
        "message_id": 100,
        "accessible": True,
        "system_account": False,
        "date": (NOW - timedelta(hours=age)).isoformat(),
        "observed_at": NOW.isoformat(),
        "text": AD,
        "warning_reply_ids": [],
    }


def snapshot(**kwargs):
    return dict(
        decision="observe",
        reason="group_rules_ai_consensus_failed",
        ai_review_incomplete=True,
        quality_status="qualified",
        evidence=[evidence()],
        collected_at=NOW.isoformat(),
        **kwargs,
    )


def test_maturity_exact_deadline_does_not_approve():
    snap = snapshot()
    assert service.review_schedule(snap, {}, NOW)[::3] == ("observe", NOW + timedelta(hours=1))
    assert snap["review_trigger"] == "ordinary_ad_maturity"


def test_maturity_respects_edits_and_known_wait():
    snap = snapshot()
    snap["evidence"][0]["edited_at"] = (NOW - timedelta(hours=3)).isoformat()
    assert evidence_maturity(snap, NOW) == NOW + timedelta(hours=21)
    snap["evidence"][0].pop("edited_at")
    snap["permissions"] = {"temporary_until": (NOW + timedelta(hours=4)).isoformat()}
    assert service.review_schedule(snap, {}, NOW)[3] == NOW + timedelta(hours=4)


@pytest.mark.parametrize(
    "change",
    [
        {"system_account": True},
        {"sender_role": "admin"},
        {"accessible": False},
        {"topic_id": 3},
        {"warning_reply_ids": [4]},
        {"date": "invalid"},
        {"sender_id": None},
        {"message_id": None},
    ],
)
def test_invalid_anchor_never_schedules_maturity(change):
    snap = snapshot()
    snap["evidence"][0].update(change)
    assert evidence_maturity(snap, NOW) is None


def test_matured_while_queued_requires_live_read_without_timer_loop():
    snap = snapshot()
    assert evidence_maturity(snap, NOW + timedelta(hours=2)) == NOW + timedelta(hours=2, seconds=1)
    snap["evidence"][0]["observed_at"] = (NOW + timedelta(hours=2)).isoformat()
    assert evidence_maturity(snap, NOW + timedelta(hours=2)) is None


def test_budget_resume_and_permission_backoff():
    snap = snapshot(collection_progress={"pending_message_ids": [5]})
    snap["evidence"] = []
    assert service.review_schedule(snap, {}, NOW)[3] == NOW + timedelta(minutes=15)
    snap["collection_progress"] = {
        "permission_failures": {"42": {"retry_at": (NOW + timedelta(hours=8)).isoformat()}}
    }
    snap["identity_errors"] = {"42": "ChatAdminRequiredError"}
    assert service.review_schedule(snap, {}, NOW)[3] == NOW + timedelta(hours=8)


class ResumeClient(ReadOnlyClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.by_id = {item.id: item for item in self.history}
        self.target_reads = []

    async def get_messages(self, entity, *, ids):
        self.target_reads.append(ids)
        return (
            [self.by_id.get(value) for value in ids]
            if isinstance(ids, list)
            else self.by_id.get(ids)
        )

    async def iter_messages(self, entity, **kwargs):
        if "reply_to" in kwargs:
            return
        async for item in super().iter_messages(entity, **kwargs):
            yield item


def resumed(client):
    collector = EvidenceCollector(client)
    collector.previous = {
        "collected_at": (NOW - timedelta(hours=2)).isoformat(),
        "evidence": [evidence()],
    }
    return collector


@pytest.mark.asyncio
async def test_live_anchor_skips_history_but_never_proves_absence():
    client = ResumeClient([message(100, sender=42, age=25, text=AD, replies=Obj(replies=0))])
    result = await resumed(client).collect(ENTITY, now=NOW)
    assert result["root_ad_history_sufficient"]
    assert not result["history_complete"] and not result["coverage"]
    assert client.permission_calls[:2] == ["me", 42]
    assert not [kw for kind, kw in client.calls if kind == "messages" and "filter" not in kw]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["deleted", "edited", "admin", "system"])
async def test_resume_refreshes_visibility_age_and_role(case):
    ad = message(100, sender=42, age=25, text=AD, replies=Obj(replies=0))
    if case == "edited":
        ad.edit_date = NOW - timedelta(hours=1)
    client = ResumeClient([] if case == "deleted" else [ad], admins={42} if case == "admin" else {})
    collector = resumed(client)
    if case == "system":
        collector.own_user_ids = {42}
    result = await collector.collect(ENTITY, now=NOW)
    assert not trial_evidence(result["evidence"])


@pytest.mark.asyncio
async def test_budget_unfinished_sender_is_read_first_next_time(monkeypatch):
    import app.modules.acquisition.group_qualification as module

    monkeypatch.setattr(module, "MAX_IDENTITIES", 2)
    history = [message(5 - i, sender=10 + i, age=1 + i / 10) for i in range(5)]
    first = await EvidenceCollector(ResumeClient(history)).collect(ENTITY, now=NOW)
    pending = first["collection_progress"]["pending_message_ids"]
    assert pending
    client = ResumeClient(history)
    collector = EvidenceCollector(client)
    collector.previous = first
    second = await collector.collect(ENTITY, now=NOW + timedelta(minutes=15))
    assert client.target_reads[0] == pending
    assert second["roles"][str(client.by_id[pending[0]].sender_id)] == "ordinary"
    assert pending[0] not in second["collection_progress"]["pending_message_ids"]


@pytest.mark.asyncio
async def test_permission_failures_are_bounded_and_cache_is_sender_scoped():
    history = [message(20 - i, sender=100 + i) for i in range(10)]
    client = ResumeClient(history)
    client.role_exception = ChatAdminRequiredError(request=None)
    first = await EvidenceCollector(client).collect(ENTITY, now=NOW)
    assert first["permission_queries"] == 3
    client2 = ResumeClient(history)
    collector = EvidenceCollector(client2)
    collector.previous = first
    result = await collector.collect(ENTITY, now=NOW + timedelta(minutes=15))
    denied = set(map(int, first["collection_progress"]["permission_failures"]))
    assert not denied.intersection(value for value in client2.permission_calls if value != "me")
    assert result["roles"]["109"] == "ordinary"


@pytest.mark.asyncio
async def test_expired_permission_backoff_probes_again():
    client = ResumeClient([message(3, sender=42)])
    collector = EvidenceCollector(client)
    collector.previous = {
        "collected_at": (NOW - timedelta(hours=3)).isoformat(),
        "collection_progress": {
            "permission_failures": {
                "42": {"attempts": 1, "retry_at": (NOW - timedelta(hours=1)).isoformat()}
            }
        },
    }
    result = await collector.collect(ENTITY, now=NOW)
    assert 42 in client.permission_calls
    assert not result["collection_progress"]["permission_failures"]


def test_candidate_expiry_account_and_group_binding():
    group = Obj(id=1, username="a", group_id=-1000000000123)
    hint = CandidatePreview(
        "sampled", peer_namespace="channel", member_count=100, ordinary_advertisers=1
    )
    fact = inventory.record(group, 2, hint, NOW)
    assert inventory.ready(fact) and inventory.fresh(fact, group, 2, NOW)
    assert not inventory.fresh(fact, group, 3, NOW)
    assert not inventory.fresh(fact, Obj(**{**vars(group), "username": "b"}), 2, NOW)
    assert inventory.fresh(fact, group, 2, NOW + timedelta(minutes=48))
    assert not inventory.fresh(fact, group, 2, NOW + timedelta(hours=3))


@pytest.mark.asyncio
async def test_unknown_candidates_rotate_and_positive_cache_is_reused(test_db, monkeypatch):
    import app.modules.acquisition.automation as module

    monkeypatch.setattr(service, "policy", AsyncMock(return_value={"enabled": True}))
    monkeypatch.setattr(module, "_now", lambda: NOW)
    hints = AsyncMock(
        return_value=CandidatePreview("sampled", peer_namespace="channel", member_count=100)
    )
    monkeypatch.setattr(module, "public_candidate_preview", hints)
    groups = [
        Group(group_id=-1000000000100 - i, username=f"candidate_{i}", status="pending_join")
        for i in range(9)
    ]
    test_db.add_all(groups)
    await test_db.commit()
    pool = Obj(acquire_by_id=AsyncMock(return_value=Obj(client=Obj())), release=AsyncMock())
    auto = AcquisitionAutomationService(test_db, account_pool=pool)
    await auto._rank_pending_join_candidates(2, groups)
    assert hints.await_count == 6
    hints.return_value = CandidatePreview(
        "sampled", peer_namespace="channel", member_count=100, ordinary_advertisers=1
    )
    ranked = await auto._rank_pending_join_candidates(2, groups)
    assert hints.await_count == 9 and [g.id for g in ranked[:3]] == [g.id for g in groups[6:]]
    await auto._rank_pending_join_candidates(2, groups)
    assert hints.await_count == 9


@pytest.mark.asyncio
async def test_capacity_separates_total_fresh_pending_and_excluded(test_db):
    from app.modules.acquisition.capacity import capacity_snapshot
    from tests.unit.test_dynamic_outbound_capacity import account_config

    account, _ = await account_config(test_db)
    test_db.add(SystemSetting(key="automation.group_qualification", value='{"enabled":true}'))
    groups = [
        Group(group_id=-1000000888800 - i, username=f"fresh_{i}", status="pending_join")
        for i in range(3)
    ]
    test_db.add_all(groups)
    await test_db.commit()
    for g, hint in zip(
        groups,
        [
            CandidatePreview(
                "sampled", peer_namespace="channel", member_count=100, rule_signal="explicit_allow"
            ),
            CandidatePreview("sampled", peer_namespace="channel", member_count=49),
        ],
        strict=False,
    ):
        await inventory.save(test_db, account.id, g.id, inventory.record(g, account.id, hint, NOW))
    await test_db.commit()
    result = (await capacity_snapshot(test_db, account.id, NOW))["workload"]
    assert [
        result[k]
        for k in [
            "join_candidates_total",
            "join_candidates",
            "join_candidates_preview_pending",
            "join_candidates_excluded",
        ]
    ] == [3, 1, 1, 1]


def test_maturity_survives_observation_endpoint_without_qualifying_or_leaving():
    snap = snapshot()
    snap["ai_review_incomplete"] = False
    snap["ordinary_advertising_last_48h_count"] = 1
    decision, _, _, retry = service.review_schedule(
        snap, {"observation_started_at": (NOW - timedelta(hours=25)).isoformat()}, NOW
    )
    assert decision == "observe" and retry == NOW + timedelta(hours=1)
