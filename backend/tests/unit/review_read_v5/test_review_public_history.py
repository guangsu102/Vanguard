"""Shared public pages discover IDs; B still proves its own current facts."""
import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as Obj

import pytest
from telethon._updates.messagebox import ENTRY_ACCOUNT
from telethon.tl import types

from app.modules.acquisition import review_history_progress as pages
from app.modules.acquisition import review_public_history as public
from app.modules.acquisition.group_qualification import EvidenceCollector, trial_evidence
from app.modules.acquisition.review_evidence_store import EvidenceStoreConfig, ReviewEvidenceStore
from tests.unit.test_group_qualification_sampling import ReadOnlyClient, message, NOW


@pytest.fixture
def env(tmp_path, monkeypatch):
    public._references.clear()
    now = datetime.now(UTC).replace(tzinfo=None)-timedelta(seconds=1)
    monkeypatch.setattr("app.modules.acquisition.review_read_flags.stage_enabled",
                        lambda _account, stage: stage in {"reuse", "resume", "public_share"})
    monkeypatch.setattr("app.modules.acquisition.review_evidence_ingest.cache_safe", lambda _: True)

    async def current_permissions(client, entity, user, **_):
        return await client.get_permissions(entity, user)
    monkeypatch.setattr("app.modules.acquisition.qualification_reads.current_permissions", current_permissions)
    entity = types.Channel(id=42, title="synthetic", photo=types.ChatPhotoEmpty(), date=now,
        username="synthetic_public", megagroup=True, creator=False, left=False, broadcast=False,
        forum=False, min=False, restricted=False, access_hash=7)
    store = ReviewEvidenceStore(EvidenceStoreConfig(directory=tmp_path / "fixed",
        enabled=True, protected_slots=1, protected_slot_bytes=64*1024, protected_control_bytes=4096,
        protected_records_per_slot=8, protected_record_limit=8, slots_per_account=1,
        slots_per_account_group=1, ordinary_pages=1, ordinary_page_bytes=1024*1024,
        physical_limit_bytes=4*1024*1024))
    assert store.initialize_sync().ok

    def msg(number=500, *, age=30, **kwargs):
        result = message(number, age=age, sender=2, text="特价服务出售 联系 https://synthetic.example", **kwargs)
        result.date += now-NOW
        result.replies = Obj(replies=0)
        return result

    class PublicClient(ReadOnlyClient):
        def __init__(self, history=(), **kwargs):
            super().__init__(history, **kwargs)
            self.full = types.ChannelFull(id=42, about="群聊介绍", read_inbox_max_id=0,
                read_outbox_max_id=0, unread_count=0, chat_photo=None, notify_settings=None,
                bot_info=[], pts=30, hidden_prehistory=False, available_min_id=0,
                participants_count=50, online_count=2)
            self.entity = copy.copy(entity)
            self._message_box = Obj(seq=10, map={ENTRY_ACCOUNT: Obj(pts=20), 42: Obj(pts=30)},
                                    getting_diff_for=set(), possible_gaps={})
            self._sender = Obj(_connection=Obj())
            self.session = Obj(journal=Obj(facts=lambda **_: []))
            self.targeted = []
            self.replies = []

        async def __call__(self, request):
            self.calls.append(("full", type(request).__name__))
            return Obj(full_chat=self.full, chats=[self.entity])

        async def get_messages(self, entity, ids):
            wanted = ids if isinstance(ids, list) else [ids]
            self.targeted.append(tuple(wanted))
            by_id = {item.id: item for item in self.history}
            values = [by_id.get(mid) for mid in wanted]
            return values if isinstance(ids, list) else values[0]

        async def iter_messages(self, entity, **kwargs):
            if "reply_to" in kwargs:
                self.calls.append(("replies", kwargs))
                for item in self.replies[:kwargs["limit"]]:
                    yield item
            else:
                async for item in super().iter_messages(entity, **kwargs):
                    yield item

    def collector(client, account_id, *, fast=False):
        scope = {"membership_id": account_id, "joined_at": (now-timedelta(days=5)).isoformat(),
                 "event": {}, "policy": "test", "profile": "p", "system_identity": [200]}
        return EvidenceCollector(client, account_id=account_id, history_scope=scope,
            history_store=store, enable_history_resume=True, enable_fast_path=fast,
            enable_on_demand_identity=False)

    return Obj(now=now, entity=entity, store=store, msg=msg, client=PublicClient, collector=collector,
               directory=tmp_path, monkeypatch=monkeypatch)


def history_calls(client):
    return [kwargs for kind, kwargs in client.calls if kind == "messages" and "filter" not in kwargs]


async def source(env):
    client = env.client([env.msg()])
    collector = env.collector(client, 2)
    result = await collector.collect(env.entity, now=env.now)
    assert result["history_complete"] and public._references, (
        collector.enable_history_resume, bool(collector.history_scope),
        [row.get("page_key", "missing") for row in result["collection_progress"]["history_cursors"]],
        pages.diagnostics())
    return client, collector, result


@pytest.mark.asyncio
async def test_a_page_provides_b_only_ids_and_b_rechecks_live_root_author_warning(env):
    _, _, original = await source(env)
    b = env.client([env.msg()])
    collector = env.collector(b, 34, fast=True)
    result = await collector.collect(env.entity, now=env.now)
    assert collector.callback_message_ids == [500]
    assert b.targeted == [(500,)] and b.permission_calls == ["me", 2]
    assert not history_calls(b) and trial_evidence(result["evidence"])
    assert result["history_complete"] is False and result["coverage"] == []
    assert result["sample_count"] == 0 and result["valid_messages_complete"] is False
    assert result["history_evaluation_status"] == "NOT_EVALUATED"
    assert original["history_complete"] is True
    assert all(not hasattr(ref, "body") and not hasattr(ref, "roles") for ref in public._references.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["deleted", "edited", "admin", "warning"])
async def test_b_changed_live_fact_rejects_source_trial_and_falls_back_to_own_history(env, change):
    await source(env)
    current = env.msg()
    b = env.client([] if change == "deleted" else [current], admins={2} if change == "admin" else ())
    if change == "edited":
        current.edit_date = env.now-timedelta(hours=1)
    elif change == "warning":
        current.replies.replies = 1
        warning = env.msg(501, age=1)
        warning.message = "禁止广告，警告并删除"
        warning.sender_id, warning.sender = 3, Obj(id=3, bot=False)
        warning.reply_to = Obj(reply_to_msg_id=500, reply_to_top_id=None, forum_topic=False)
        b.replies = [warning]
        b.history.insert(0, warning)
        b.admins.add(3)
    collector = env.collector(b, 34, fast=True)
    result = await collector.collect(env.entity, now=env.now)
    assert collector.callback_message_ids == [500] and b.targeted
    assert history_calls(b), "A's coverage must not replace the fallback reader"
    assert not trial_evidence(result["evidence"])
    if change == "admin":
        assert result["roles"]["2"] == "admin"
    if change == "warning":
        assert any(item.get("warning_reply_ids") for item in result["evidence"])
    # Compare the original reader on the same B-visible facts, not A's verdict.
    env.monkeypatch.setattr("app.modules.acquisition.review_read_flags.stage_enabled",
                           lambda _account, stage: stage in {"reuse", "resume"})
    baseline_client = env.client(copy.deepcopy(b.history), admins=set(b.admins))
    baseline_client.replies = copy.deepcopy(b.replies)
    baseline = await env.collector(baseline_client, 34, fast=True).collect(env.entity, now=env.now)

    def business(snapshot):
        evidence = sorted((item.get("source"), item.get("message_id") or 0,
                           item.get("sender_role"), item.get("text"),
                           tuple(item.get("warning_reply_ids", []))) for item in snapshot["evidence"])
        return (snapshot["quality_status"], snapshot["quality_reason"], snapshot["permissions"],
                trial_evidence(snapshot["evidence"]), snapshot["valid_messages"], evidence)
    assert business(result) == business(baseline)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["private", "forum", "min", "hidden", "available_min", "pts",
    "gap", "pending", "ingest", "member", "admin", "view_messages", "canonical", "flag"])
async def test_visibility_or_protocol_failure_returns_no_cross_account_ids(env, change):
    await source(env)
    client = env.client([env.msg()])
    collector = env.collector(client, 34)
    collector.full_chat, collector.full_entity = client.full, client.entity
    collector.self_history_visible = True
    result = {"permissions": {"member": True, "is_admin": False, "is_creator": False}, "technical_errors": []}
    if change == "private":
        client.entity.username = None
    elif change == "forum":
        client.entity.forum = True
    elif change == "min":
        client.entity.min = True
    elif change == "hidden":
        client.full.hidden_prehistory = True
    elif change == "available_min":
        client.full.available_min_id = 10
    elif change == "pts":
        client._message_box.map[42].pts += 1
    elif change == "gap":
        client._message_box.possible_gaps[42] = True
    elif change == "pending":
        client.session.journal.facts = lambda **_: [("x", '{"kind":"rules","peer":-1000000000042}')]
    elif change == "ingest":
        env.monkeypatch.setattr("app.modules.acquisition.review_evidence_ingest.cache_safe", lambda _: False)
    elif change == "member":
        result["permissions"]["member"] = False
    elif change == "admin":
        result["permissions"]["is_admin"] = True
    elif change == "view_messages":
        collector.self_history_visible = False
    elif change == "canonical":
        client.entity.id += 1
    else:
        env.monkeypatch.setattr("app.modules.acquisition.review_read_flags.stage_enabled", lambda *_: False)
    assert await public.candidate_ids(env.store, collector=collector, entity=env.entity,
                                      result=result, now=env.now) == []


@pytest.mark.asyncio
async def test_raw_public_hints_expire_at_300_seconds(env):
    _, _, _ = await source(env)
    b = env.client([env.msg()])
    collector = env.collector(b, 34)
    collector.full_chat, collector.full_entity, collector.self_history_visible = b.full, b.entity, True
    result = {"permissions": {"member": True, "is_admin": False, "is_creator": False}, "technical_errors": []}
    assert await public.candidate_ids(env.store, collector=collector, entity=env.entity, result=result, now=env.now) == [500]
    instant = max(ref.observed_at for ref in public._references.values())+301
    env.monkeypatch.setattr(public.time, "time", lambda: instant)
    assert await public.candidate_ids(env.store, collector=collector, entity=env.entity, result=result, now=env.now) == []


@pytest.mark.asyncio
async def test_connection_generation_changed_during_load_rejects_package(env):
    await source(env)
    b = env.client([env.msg()])
    collector = env.collector(b, 34)
    collector.full_chat, collector.full_entity, collector.self_history_visible = b.full, b.entity, True
    result = {"permissions": {"member": True, "is_admin": False, "is_creator": False}, "technical_errors": []}
    original = env.store.load_history_page

    async def changed(*args, **kwargs):
        response = await original(*args, **kwargs)
        b._sender = Obj(_connection=Obj())
        return response
    env.monkeypatch.setattr(env.store, "load_history_page", changed)
    assert await public.load(env.store, collector=collector, entity=env.entity, result=result,
                             window_end=env.now-timedelta(days=1)) is None


@pytest.mark.asyncio
async def test_evicted_source_body_cannot_become_a_history_hint(env):
    await source(env)
    assert env.store.invalidate_messages_sync(account_id=2, group_id=-1000000000042, message_ids=[500]).ok
    b = env.client([env.msg()])
    collector = env.collector(b, 34)
    collector.full_chat, collector.full_entity, collector.self_history_visible = b.full, b.entity, True
    result = {"permissions": {"member": True, "is_admin": False, "is_creator": False}, "technical_errors": []}
    assert await public.candidate_ids(env.store, collector=collector, entity=env.entity, result=result, now=env.now) == []


def test_references_are_fixed_256_scalars_and_never_copy_payload(env):
    instant = env.now.replace(tzinfo=UTC).timestamp()
    for index in range(300):
        cert = {"account_id": 2, "group_id": -1000000000042, "pts": 30, "identity": "fixed"}
        cursor = {"window_end": env.now.isoformat(), "offset_id": 1, "complete": True,
                  "page_key": f"{index:032x}", "body": "NEVER_COPY", "records": ["NEVER_COPY"]}
        public.publish(cert, cursor=cursor, observed_at=instant)
    assert len(public._references) == 256
    assert all(set(ref.cursor) == {"window_end", "offset_id", "complete", "page_key"}
               and "NEVER_COPY" not in str(ref) for ref in public._references.values())


@pytest.mark.asyncio
async def test_reading_shared_hints_creates_no_new_payload_files(env):
    await source(env)
    before = {path.relative_to(env.directory): path.stat().st_size for path in env.directory.rglob("*") if path.is_file()}
    client = env.client([env.msg()])
    collector = env.collector(client, 34)
    collector.full_chat, collector.full_entity, collector.self_history_visible = client.full, client.entity, True
    result = {"permissions": {"member": True, "is_admin": False, "is_creator": False}, "technical_errors": []}
    assert await public.candidate_ids(env.store, collector=collector, entity=env.entity, result=result, now=env.now) == [500]
    after = {path.relative_to(env.directory): path.stat().st_size for path in env.directory.rglob("*") if path.is_file()}
    assert after == before
