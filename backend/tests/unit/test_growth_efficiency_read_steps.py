"""Payable steps preserve raw progress while live proofs remain scoped."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as Obj

import pytest

from app.modules.acquisition import qualification_engine as engine
from app.modules.acquisition.group_qualification import EvidenceCollector
from app.modules.acquisition.review_read_plan import step_plan, for_join
from app.modules.acquisition.review_evidence_store import EvidenceStoreConfig, ReviewEvidenceStore
from tests.unit.test_group_qualification_sampling import ENTITY, NOW, ReadOnlyClient, message


@pytest.fixture(autouse=True)
def efficiency(monkeypatch):
    monkeypatch.setattr(engine, "enabled_for", lambda account_id: account_id == 33)


class Client(ReadOnlyClient):
    async def get_messages(self, entity, ids):
        self.calls.append(("targeted", ids))
        found = {m.id: m for m in self.history}
        return [found.get(i) for i in ids] if isinstance(ids, (list, tuple)) else found.get(ids)


def msg(number, now, **kwargs):
    value = message(number, **kwargs)
    value.date += now-NOW
    return value


@pytest.mark.asyncio
async def test_cold_discovery_is_one_page_without_chat_identity_lookups():
    now = datetime.now(UTC).replace(tzinfo=None)
    client = Client([msg(1000-i, now, age=i/100, sender=i+1) for i in range(500)])
    result = await EvidenceCollector(client, account_id=33).collect(ENTITY, now=now)
    history = [kw for kind, kw in client.calls if kind == "messages" and "filter" not in kw]
    assert len(history) == 1 and result["sample_count"] == 100
    assert client.permission_calls == ["me"]
    assert result["business_maintenance_deferred"]
    assert not result["discovery_exhausted"]


@pytest.mark.asyncio
async def test_mature_candidate_uses_72h_window_and_ignores_business_counts():
    now = datetime.now(UTC).replace(tzinfo=None)
    advert = msg(50, now, age=60, sender=7, text="特价服务出售欢迎联系 https://ads.example",
                 replies=Obj(replies=0))
    client = Client([advert], count=3)
    client.full.online_count = None
    collector = EvidenceCollector(client, account_id=33)
    collector.callback_message_ids = [50]
    result = await collector.collect(ENTITY, now=now)
    assert result["root_ad_history_sufficient"]
    assert result["decision_dependencies_complete"]
    assert result["member_count"] == 3 and result["online_count"] is None
    assert not [kw for kind, kw in client.calls if kind == "messages"]


@pytest.mark.asyncio
async def test_reconnect_keeps_raw_page_and_cursor_but_refreshes_permissions(tmp_path):
    now = datetime.now(UTC).replace(tzinfo=None)-timedelta(seconds=1)
    scope = {"membership_id": 91, "joined_at": (now-timedelta(days=5)).isoformat(),
             "event": {}, "policy": "v", "profile": "p", "system_identity": [1]}
    store = ReviewEvidenceStore(EvidenceStoreConfig(directory=tmp_path/"evidence", enabled=True,
        protected_slots=1, protected_slot_bytes=65536, protected_control_bytes=4096,
        protected_records_per_slot=8, protected_record_limit=8, slots_per_account=1,
        slots_per_account_group=1, ordinary_pages=1, ordinary_page_bytes=1024*1024,
        physical_limit_bytes=4*1024*1024))
    assert store.initialize_sync().ok
    history = [msg(1000-i, now, age=1+i/100) for i in range(250)]
    client = Client(history)
    first = EvidenceCollector(client, account_id=33, history_scope=scope, history_store=store)
    snapshot = await first.collect(ENTITY, now=now)
    assert snapshot["collection_progress"]["history_cursors"][0].get("page_key") not in {None, "unavailable"}
    reconnected = Client(history)
    second = EvidenceCollector(reconnected, account_id=33, history_scope=scope, history_store=store)
    second.previous = snapshot
    result = await second.collect(ENTITY, now=now+timedelta(minutes=20))
    history_calls = [kw for kind, kw in reconnected.calls if kind == "messages" and "filter" not in kw]
    assert len(history_calls) == 1 and history_calls[0]["offset_id"] == 901
    assert result["history_cached_count"] == 100
    assert reconnected.permission_calls == ["me"]
    assert not [kw for kind, kw in reconnected.calls if kind == "full"]
    assert not result["history_complete"]  # Raw content never proves current absence.


def test_waiting_obligations_have_no_telegram_plan():
    now = datetime.now()
    member = Obj(id=3, joined_at=now-timedelta(days=2))
    assert step_plan({"maturity_at": (now+timedelta(hours=2)).isoformat()}, member, now).reads == 0
    assert step_plan({"ai_pending": True, "collection_complete": True}, member, now).reads == 0
    assert step_plan({"discovery_exhausted": True}, member, now).reads == 0
    assert step_plan({"discovery_exhausted": True}, member, now, event={"kinds": ["rules"]}).reads == 8


@pytest.mark.asyncio
async def test_join_plan_has_no_future_review_lifecycle_debt():
    value = await for_join(None, 33, 4, datetime.now())
    assert 0 < value.required_reads <= 8
    assert "history" not in value.missing_dependencies
    cached = await for_join(None, 33, 4, datetime.now(), candidate={"status": "metadata_checked"})
    assert cached.required_reads < value.required_reads


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["admin", "oversized_thread"])
async def test_unusable_candidate_does_not_hide_second_ordinary_candidate(tmp_path, scenario):
    now = datetime.now(UTC).replace(tzinfo=None)-timedelta(seconds=1)
    scope = {"membership_id": 91, "joined_at": (now-timedelta(days=5)).isoformat(),
             "event": {}, "policy": "v", "profile": "p", "system_identity": [1]}
    store = ReviewEvidenceStore(EvidenceStoreConfig(directory=tmp_path/"evidence", enabled=True,
        protected_slots=1, protected_slot_bytes=65536, protected_control_bytes=4096,
        protected_records_per_slot=8, protected_record_limit=8, slots_per_account=1,
        slots_per_account_group=1, ordinary_pages=1, ordinary_page_bytes=1024*1024,
        physical_limit_bytes=4*1024*1024))
    assert store.initialize_sync().ok
    client = Client([
        msg(50, now, age=40, sender=8, text="促销出售 https://first.example",
            replies=Obj(replies=600 if scenario == "oversized_thread" else 0)),
        msg(40, now, age=30, sender=7, text="特价服务出售欢迎联系 https://ordinary.example", replies=Obj(replies=0)),
    ], admins={8} if scenario == "admin" else set())
    previous = {}
    for _ in range(5):
        collector = EvidenceCollector(client, account_id=33, history_scope=scope, history_store=store)
        collector.previous = previous
        previous = await collector.collect(ENTITY, now=now)
        if previous.get("root_ad_history_sufficient"):
            break
    assert previous["root_ad_history_sufficient"]
    assert any(item.get("sender_id") == 7 and item.get("sender_role") == "ordinary"
               for item in previous["evidence"])
    assert client.permission_calls.count(8) == 1
