"""Exact-scope reuse must reduce RPCs without manufacturing trial evidence."""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj

import pytest

from app.modules.acquisition.group_qualification import EvidenceCollector
from app.modules.acquisition.qualification_read_context import QualificationReadContext

NOW = datetime(2026, 11, 3, 12)
ENTITY = Obj(id=42, megagroup=True)


def root(message_id=7, *, replies=0, edited=None):
    return Obj(
        id=message_id, sender_id=2, sender=Obj(id=2, bot=False),
        message="优惠服务出售欢迎联系 https://one.example", date=NOW - timedelta(hours=30),
        edit_date=edited, replies=Obj(replies=replies) if replies is not None else None,
    )


class Client:
    def __init__(self, messages=(), *, reply_threads=None):
        self.messages = {message.id: message for message in messages}
        self.reply_threads = reply_threads or {}
        self.calls = []
        self.permissions = []
        self.role_error = None

    async def get_messages(self, entity, ids):
        self.calls.append(ids[:] if isinstance(ids, list) else ids)
        await asyncio.sleep(0)
        if isinstance(ids, list):
            return [self.messages.get(identifier) for identifier in ids]
        return self.messages.get(ids)

    async def get_permissions(self, entity, sender):
        self.permissions.append((entity.id, sender.id))
        if self.role_error is not None:
            raise self.role_error
        return Obj(is_admin=False, is_creator=False, has_left=False, is_banned=False,
                   participant=Obj(banned_rights=None))

    async def iter_messages(self, entity, **kwargs):
        self.calls.append(("replies", kwargs["reply_to"]))
        for message in self.reply_threads.get(kwargs["reply_to"], [])[:kwargs["limit"]]:
            yield message


def advert(message):
    return {"message_id": message.id, "text": message.message, "accessible": True}


def result():
    return {"unknowns": [], "technical_errors": [], "rules_incomplete": False}


@pytest.mark.asyncio
async def test_overlap_is_batched_once_and_missing_message_stays_known_absent():
    messages = [root(7), root(8), root(9)]
    client = Client(messages)
    context = QualificationReadContext()

    first = await context.get_messages(client, ENTITY, [7, 7, 8])
    second = await context.get_messages(client, ENTITY, [8, 9, 404])
    assert [message.id for message in first] == [7, 8]
    assert [message.id if message else None for message in second] == [8, 9, None]
    assert await context.get_message(client, ENTITY, 404) is None
    assert client.calls == [[7, 8], [9, 404]]


@pytest.mark.asyncio
async def test_parallel_consumers_do_not_duplicate_the_same_targeted_read():
    client = Client([root(7), root(8)])
    context = QualificationReadContext()
    first, second = await asyncio.gather(
        context.get_messages(client, ENTITY, [7]),
        context.get_messages(client, ENTITY, [7, 8]),
    )
    assert first[0].id == second[0].id == 7
    assert client.calls == [[7], [8]]


@pytest.mark.asyncio
async def test_client_namespace_and_new_event_do_not_share_message_visibility():
    first, second = Client([root()]), Client()
    context = QualificationReadContext()
    assert (await context.get_message(first, ENTITY, 7)).id == 7
    assert await context.get_message(second, ENTITY, 7) is None
    await context.get_message(first, Obj(id=42), 7)
    await QualificationReadContext().get_message(first, ENTITY, 7)
    assert first.calls == [7, 7, 7]
    assert second.calls == [7]


@pytest.mark.asyncio
async def test_bounded_cache_still_returns_the_entire_current_response():
    client = Client([root(7), root(8), root(9)])
    context = QualificationReadContext(max_messages=1)
    values = await context.get_messages(client, ENTITY, [7, 8, 9])
    assert [message.id for message in values] == [7, 8, 9]
    assert not context.cached_message(client, ENTITY, 7)[0]
    assert context.cached_message(client, ENTITY, 9)[0]
    assert client.calls == [[7, 8, 9]]


@pytest.mark.asyncio
async def test_resumed_fresh_root_and_explicit_zero_replies_need_no_second_read():
    message = root()
    client = Client([message])
    collector = EvidenceCollector(client)
    collector.previous = {"evidence": [{
        "message_id": message.id, "source": "recent_promotional_message",
        "sender_role": "ordinary", "accessible": True,
    }]}
    evidence = result()
    assert (await collector._resume_messages(ENTITY, evidence, NOW))[0].id == message.id
    proof = advert(message)
    await collector._verify_warning_thread(ENTITY, proof, NOW, evidence)
    assert client.calls == [[message.id]]
    assert proof["warning_search_complete"] is True
    assert proof["warning_reply_ids"] == []


@pytest.mark.asyncio
async def test_unknown_reply_count_does_not_become_a_complete_empty_thread():
    message = root(replies=None)
    client = Client([message])
    proof, evidence = advert(message), result()
    await EvidenceCollector(client)._verify_warning_thread(ENTITY, proof, NOW, evidence)
    assert not proof.get("warning_search_complete")
    assert "warning_reply_count_unconfirmed" in evidence["unknowns"]
    assert client.calls == [message.id]


@pytest.mark.asyncio
async def test_nonempty_thread_retains_warning_and_complete_count_requirement():
    message = root(replies=1)
    warning = Obj(id=20, message="警告，请删除这条广告")
    client = Client([message], reply_threads={message.id: [warning]})
    context = QualificationReadContext()
    await context.get_messages(client, ENTITY, [message.id])
    proof, evidence = advert(message), result()
    await EvidenceCollector(client, read_context=context)._verify_warning_thread(
        ENTITY, proof, NOW, evidence,
    )
    assert proof["warning_reply_ids"] == [warning.id]
    assert proof["warning_search_complete"] is True
    assert client.calls == [[message.id], ("replies", message.id)]


@pytest.mark.asyncio
async def test_fresh_root_recent_edit_still_prevents_mature_proof():
    old, edited = root(), root(edited=NOW - timedelta(hours=1))
    client = Client([edited])
    proof, evidence = advert(old), result()
    await EvidenceCollector(client)._verify_warning_thread(ENTITY, proof, NOW, evidence)
    assert proof["age_hours"] == 1
    assert not proof.get("warning_search_complete")
    assert client.calls == [old.id]


@pytest.mark.asyncio
async def test_two_collectors_share_only_successful_roles_in_the_same_client_group():
    client = Client()
    context = QualificationReadContext()
    assert await EvidenceCollector(client, read_context=context).role(ENTITY, root()) == "ordinary"
    second = EvidenceCollector(client, read_context=context)
    assert await second.role(ENTITY, root()) == "ordinary"
    assert second.permission_queries == 0
    assert client.permissions == [(42, 2)]
    # System-account exclusions must precede a previously shared ordinary role.
    excluded = EvidenceCollector(client, own_user_ids={2}, read_context=context)
    assert await excluded.role(ENTITY, root()) == "system"
    await EvidenceCollector(client, read_context=context).role(Obj(id=43, megagroup=True), root())
    another = Client()
    await EvidenceCollector(another, read_context=context).role(ENTITY, root())
    assert client.permissions == [(42, 2), (43, 2)]
    assert another.permissions == [(42, 2)]


@pytest.mark.asyncio
async def test_role_failure_never_becomes_shared_ordinary_or_permanent_shared_unknown():
    client = Client()
    context = QualificationReadContext()
    client.role_error = RuntimeError("lookup temporarily unavailable")
    first = EvidenceCollector(client, read_context=context)
    assert await first.role(ENTITY, root()) == "unknown"
    assert context.role(client, ENTITY, 2) is None
    client.role_error = None
    # An exact transient failure is shared for this short event backoff; it
    # never becomes an ordinary role or a permanent cached unknown.
    assert await EvidenceCollector(client, read_context=context).role(ENTITY, root()) == "unknown"
    context.invalidate()
    assert await EvidenceCollector(client, read_context=context).role(ENTITY, root()) == "ordinary"
    assert client.permissions == [(42, 2), (42, 2)]


class FastPathClient(Client):
    def __init__(self, messages=(), *, members=50, online=2, reply_threads=None):
        super().__init__(messages, reply_threads=reply_threads)
        self.full = Obj(participants_count=members, online_count=online,
                        about="本群禁止广告", hidden_prehistory=False)
        self.iter_calls = []
        self.own_rights = None
        self.admins = set()

    async def __call__(self, request):
        if type(request).__name__ == "GetOnlinesRequest":
            return Obj(onlines=self.full.online_count)
        return Obj(full_chat=self.full)

    async def get_permissions(self, entity, sender):
        if sender == "me":
            return Obj(is_admin=False, is_creator=False, has_left=False, is_banned=False,
                       participant=Obj(banned_rights=self.own_rights))
        permission = await super().get_permissions(entity, sender)
        permission.is_admin = sender.id in self.admins
        return permission

    async def iter_messages(self, entity, **kwargs):
        self.iter_calls.append(kwargs)
        if "reply_to" in kwargs:
            values = self.reply_threads.get(kwargs["reply_to"], [])
        elif "filter" in kwargs:
            values = []
        else:
            values = sorted(self.messages.values(), key=lambda item: item.id, reverse=True)
            end = kwargs.get("offset_date")
            if end:
                values = [item for item in values if item.date < end.replace(tzinfo=None)]
            offset = kwargs.get("offset_id", 0)
            values = [item for item in values if not offset or item.id < offset]
        for message in values[:kwargs["limit"]]:
            yield message


@pytest.mark.asyncio
async def test_sufficient_trial_proof_stops_before_pins_and_history_even_with_general_ban():
    from app.modules.acquisition.group_qualification import trial_proof

    client = FastPathClient([root()])
    collector = EvidenceCollector(client, own_user_ids={999}, enable_fast_path=True)
    collector.callback_message_ids = [7]
    snapshot = await collector.collect(ENTITY, now=NOW)

    assert snapshot["sampling_stopped_reason"] == "sufficient_ordinary_root_trial_proof"
    assert snapshot["rules_evaluation_status"] == "NOT_EVALUATED"
    assert snapshot["history_evaluation_status"] == "NOT_EVALUATED"
    assert snapshot["rules_incomplete"] is True
    assert snapshot["history_complete"] is False
    assert snapshot["decision_dependencies_complete"] is True
    assert snapshot["collection_complete"] is True
    assert snapshot["exit_group_rule_ban"] is None
    assert snapshot["valid_messages_complete"] is False
    assert snapshot["ordinary_advertising_last_48h_count"] == 1
    assert snapshot["quality_status"] == "qualified"
    assert snapshot["evidence"][0]["text"] == "本群禁止广告"
    assert trial_proof(snapshot["evidence"])
    assert client.calls == [[7]]
    assert client.permissions == [(42, 2)]
    assert client.iter_calls == []


@pytest.mark.asyncio
async def test_fast_path_is_disabled_by_default_and_has_no_initial_candidate_sweep():
    for enabled, hints in ((False, [7]), (True, [])):
        client = FastPathClient([root()])
        collector = EvidenceCollector(client, enable_fast_path=enabled)
        collector.callback_message_ids = hints
        snapshot = await collector.collect(ENTITY, now=NOW)
        assert snapshot.get("sampling_stopped_reason") != "sufficient_ordinary_root_trial_proof"
        assert client.iter_calls


@pytest.mark.asyncio
async def test_first_sufficient_root_avoids_unnecessary_other_candidate_role_reads():
    first, second = root(7), root(8)
    second.sender_id, second.sender = 3, Obj(id=3, bot=False)
    client = FastPathClient([first, second])
    collector = EvidenceCollector(client, enable_fast_path=True)
    collector.callback_message_ids = [7, 8]
    snapshot = await collector.collect(ENTITY, now=NOW)
    assert snapshot["sampling_stopped_reason"] == "sufficient_ordinary_root_trial_proof"
    assert client.calls == [[7, 8]]
    assert client.permissions == [(42, 2)]
    assert client.iter_calls == []


@pytest.mark.asyncio
async def test_fast_path_preserves_unread_pending_ids_for_a_later_full_collection():
    client = FastPathClient([root(7), root(8)])
    collector = EvidenceCollector(client, enable_fast_path=True)
    collector.callback_message_ids = [7]
    collector.previous = {
        "collected_at": NOW.isoformat(),
        "collection_progress": {"pending_message_ids": [8]},
    }
    snapshot = await collector.collect(ENTITY, now=NOW)
    assert snapshot["collection_complete"] is True
    assert snapshot["collection_progress"]["pending_message_ids"] == []
    assert snapshot["collection_progress"]["not_evaluated_message_ids"] == [8]

    # Later loss of a sufficient condition restores the original work queue.
    later = FastPathClient([root(7), root(8)], online=1)
    full_collector = EvidenceCollector(later, enable_fast_path=True)
    full_collector.previous = snapshot
    resumed = await full_collector.collect(ENTITY, now=NOW)
    assert [7, 8] in later.calls
    assert resumed.get("sampling_stopped_reason") != "sufficient_ordinary_root_trial_proof"
    assert "not_evaluated_message_ids" not in resumed["collection_progress"]


@pytest.mark.parametrize("members,online", [(49, 2), (None, 2), (50, 1), (50, None)])
@pytest.mark.asyncio
async def test_fast_path_missing_exit_dependencies_returns_to_original_collection(members, online):
    client = FastPathClient([root()], members=members, online=online)
    collector = EvidenceCollector(client, enable_fast_path=True)
    collector.callback_message_ids = [7]
    snapshot = await collector.collect(ENTITY, now=NOW)
    assert snapshot.get("sampling_stopped_reason") != "sufficient_ordinary_root_trial_proof"
    assert "rules_evaluation_status" not in snapshot
    assert client.iter_calls


@pytest.mark.parametrize("condition", ["recent_edit", "old_publication", "topic", "system", "admin", "unknown_replies", "warning"])
@pytest.mark.asyncio
async def test_fast_path_never_uses_an_ineligible_root_as_sufficient_proof(condition):
    message = root()
    if condition == "recent_edit":
        message.edit_date = NOW - timedelta(hours=1)
    elif condition == "old_publication":
        message.date = NOW - timedelta(hours=60)
        message.edit_date = NOW - timedelta(hours=36)
    elif condition == "topic":
        message.reply_to = Obj(forum_topic=True, reply_to_top_id=100, reply_to_msg_id=100)
    elif condition == "unknown_replies":
        message.replies = None
    elif condition == "warning":
        message.replies = Obj(replies=1)
    client = FastPathClient([message], reply_threads={7: [Obj(id=20, message="警告，请删除广告")]})
    if condition == "admin":
        client.admins.add(2)
    collector = EvidenceCollector(client, own_user_ids={2} if condition == "system" else {999},
                                  enable_fast_path=True)
    collector.callback_message_ids = [7]
    snapshot = await collector.collect(ENTITY, now=NOW)
    assert snapshot.get("sampling_stopped_reason") != "sufficient_ordinary_root_trial_proof"
    assert any("filter" in call for call in client.iter_calls)
    if condition == "old_publication":
        # Mature 48–72h proof keeps its old use, but never proves a recent post.
        assert snapshot["root_ad_history_sufficient"] is True
        assert snapshot["ordinary_advertising_last_48h_count"] == 0


@pytest.mark.parametrize("age", [24, 48])
@pytest.mark.asyncio
async def test_fast_path_accepts_both_inclusive_publication_boundaries(age):
    message = root()
    message.date = NOW - timedelta(hours=age)
    client = FastPathClient([message])
    collector = EvidenceCollector(client, enable_fast_path=True)
    collector.callback_message_ids = [7]
    snapshot = await collector.collect(ENTITY, now=NOW)
    assert snapshot["sampling_stopped_reason"] == "sufficient_ordinary_root_trial_proof"
