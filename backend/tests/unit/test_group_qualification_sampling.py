"""Bounded, read-only sampling behavior for account-scoped group qualification."""

from datetime import datetime, timedelta
from types import SimpleNamespace as Obj

import pytest
from telethon.errors import FloodWaitError, UserNotParticipantError

from app.modules.acquisition.group_qualification import (
    EvidenceCollector,
    promotion_targets,
    trial_evidence,
)

NOW = datetime(2026, 9, 22, 12)
ENTITY = Obj(id=42, megagroup=True)


def message(number, *, age=1, text=None, sender=1, bot=False, **extra):
    values = {
        "id": number,
        "sender_id": sender,
        "sender": Obj(id=sender, bot=bot),
        "message": text if text is not None else "普通聊天消息 " + str(number),
        "date": NOW - timedelta(hours=age),
        "edit_date": None,
        "media": None,
        "action": None,
        "reply_to": None,
        "fwd_from": None,
    }
    values.update(extra)
    return Obj(**values)


class ReadOnlyClient:
    def __init__(self, history=(), *, pins=(), count=50, hidden=False, admins=()):
        self.history = sorted(history, key=lambda item: item.id, reverse=True)
        self.pins = list(pins)
        self.full = Obj(
            participants_count=count, online_count=2, about="群聊介绍", hidden_prehistory=hidden
        )
        self.admins = set(admins)
        self.calls = []
        self.permission_calls = []
        self.own_rights = None
        self.role_exception = None
        self.full_exception = None

    async def __call__(self, request):
        self.calls.append(("full", type(request).__name__))
        if self.full_exception:
            raise self.full_exception
        return Obj(full_chat=self.full)

    async def get_permissions(self, entity, user):
        self.permission_calls.append(user if user == "me" else user.id)
        if user != "me" and self.role_exception:
            raise self.role_exception
        return Obj(
            is_admin=user != "me" and user.id in self.admins,
            is_creator=False,
            participant=Obj(banned_rights=self.own_rights if user == "me" else None),
            has_left=False,
            is_banned=False,
        )

    async def get_entity(self, sender):
        return Obj(id=sender, bot=False)

    async def iter_messages(self, entity, **kwargs):
        self.calls.append(("messages", kwargs))
        source = self.pins if "filter" in kwargs else self.history
        if "filter" not in kwargs:
            end = kwargs.get("offset_date")
            if end:
                end = end.replace(tzinfo=None)
                source = [item for item in source if item.date < end]
            offset = kwargs.get("offset_id")
            if offset:
                source = [item for item in source if item.id < offset]
        for item in source[: kwargs["limit"]]:
            yield item


@pytest.mark.asyncio
async def test_busy_recent_day_does_not_hide_older_advertising():
    recent = [message(3000 - index, age=index / 100, sender=1) for index in range(1500)]
    old_ads = [
        message(500, age=30, sender=2, text="特价家具出售欢迎联系 https://furniture.example"),
        message(200, age=54, sender=3, text="鲜花批发优惠联系 https://flowers.example"),
    ]
    client = ReadOnlyClient(recent + old_ads)
    result = await EvidenceCollector(client).collect(ENTITY, now=NOW)
    history_calls = [
        kwargs for kind, kwargs in client.calls if kind == "messages" and "filter" not in kwargs
    ]
    assert [item["offset_date"].replace(tzinfo=None) for item in history_calls[:3]] == [
        NOW + timedelta(microseconds=1),
        NOW - timedelta(hours=24) + timedelta(microseconds=1),
        NOW - timedelta(hours=48) + timedelta(microseconds=1),
    ]
    assert all(item["limit"] == 100 for item in history_calls[:3])
    assert result["sample_count"] == 1000
    assert not result["history_complete"]
    assert result["coverage"][0]["unknown_reason"] == "sample_limit"
    assert result["coverage"][1]["complete"]
    assert result["coverage"][2]["complete"]
    assert not result["trial_history_sufficient"]
    assert result["quality_status"] == "qualified"


@pytest.mark.asyncio
async def test_a_thousand_duplicate_ads_cannot_prove_low_activity():
    history = [
        message(3000 - index, age=index / 100, text="低价出售服务联系 https://same.example")
        for index in range(1500)
    ]
    result = await EvidenceCollector(ReadOnlyClient(history)).collect(ENTITY, now=NOW)
    assert result["sample_count"] == 1000
    assert result["valid_messages"] == 1
    assert result["quality_status"] == "qualified"


@pytest.mark.asyncio
async def test_full_three_day_coverage_can_prove_four_messages_insufficient():
    history = [message(10, age=1), message(9, age=24), message(8, age=48), message(7, age=72)]
    result = await EvidenceCollector(ReadOnlyClient(history)).collect(ENTITY, now=NOW)
    assert result["history_complete"]
    assert result["valid_messages"] == 4
    assert result["quality_status"] == "qualified"
    assert result["member_count_source"] == "full_chat.participants_count"
    assert result["member_count_checked_at"] == NOW.isoformat()


@pytest.mark.asyncio
async def test_flood_wait_during_identity_read_stops_all_further_reads():
    client = ReadOnlyClient([message(3), message(2), message(1)])
    client.role_exception = FloodWaitError(request=None, capture=3600)
    result = await EvidenceCollector(client).collect(ENTITY, now=NOW)
    assert client.permission_calls == ["me", 1]
    assert result["collection_halted"] == "telegram_rate_limit"
    assert result["retry_after_seconds"] == 3600
    assert result["quality_status"] == "technical_wait"


@pytest.mark.asyncio
async def test_flood_wait_during_full_info_never_reads_pins_or_history():
    client = ReadOnlyClient()
    client.full_exception = FloodWaitError(request=None, capture=120)
    result = await EvidenceCollector(client).collect(ENTITY, now=NOW)
    assert len(client.calls) == 1
    assert not client.permission_calls
    assert result["retry_after_seconds"] == 120


@pytest.mark.asyncio
async def test_human_admin_chat_counts_but_admin_rules_and_bots_do_not():
    client = ReadOnlyClient(
        [
            message(5, sender=4),
            message(4, sender=4, text="本群禁止刷屏"),
            message(3, bot=True, sender=5),
            message(2, sender=6),
            message(1, sender=7),
        ],
        admins={4},
    )
    result = await EvidenceCollector(client, own_user_ids={7}).collect(ENTITY, now=NOW)
    assert result["valid_messages"] == 2
    assert result["roles"]["4"] == "admin"
    assert result["roles"]["5"] == "bot"
    assert any(item["source"] == "admin_rule" for item in result["evidence"])


@pytest.mark.asyncio
async def test_url_text_is_allowed_when_only_link_previews_are_disabled():
    entity = Obj(id=42, megagroup=True, default_banned_rights=Obj(embed_links=True))
    result = await EvidenceCollector(ReadOnlyClient()).collect(entity, now=NOW)
    permissions = result["permissions"]
    assert permissions["can_send_text"]
    assert permissions["can_send_url_text"]
    assert not permissions["can_preview_links"]
    assert not permissions["url_content_rules_checked"]


@pytest.mark.asyncio
async def test_temporary_restriction_and_slowmode_retain_exact_deadlines():
    client = ReadOnlyClient([message(number) for number in range(1, 6)])
    client.own_rights = Obj(send_plain=True, until_date=NOW + timedelta(hours=2))
    client.full.slowmode_next_send_date = NOW + timedelta(minutes=1)
    result = await EvidenceCollector(client).collect(ENTITY, now=NOW)
    assert result["quality_status"] == "wait"
    assert result["permissions"]["temporary_until"] == (NOW + timedelta(hours=2)).isoformat()
    assert result["permissions"]["slowmode_until"] == (NOW + timedelta(minutes=1)).isoformat()


@pytest.mark.asyncio
async def test_all_pins_are_retained_without_relabelling_author_as_admin():
    rule = "群规原文" + "。允许普通成员文字广告" * 400
    client = ReadOnlyClient(pins=[message(number, text=rule) for number in range(60)])
    result = await EvidenceCollector(client).collect(ENTITY, now=NOW)
    pins = [item for item in result["evidence"] if item["source"] == "pinned_message"]
    assert len(pins) == 60
    assert pins[0]["text"] == rule
    assert pins[0]["sender_role"] == "ordinary"
    assert not result["rules_incomplete"]


@pytest.mark.asyncio
async def test_pinned_cap_is_explicit_unknown_not_complete_rules():
    client = ReadOnlyClient(pins=[message(number) for number in range(201)])
    result = await EvidenceCollector(client).collect(ENTITY, now=NOW)
    assert result["pinned_sample_count"] == 200
    assert result["rules_incomplete"]
    assert "pinned_history_limit" in result["unknowns"]


@pytest.mark.asyncio
async def test_advertising_retains_edit_forward_topic_and_specific_warning():
    reply = Obj(reply_to_top_id=77, reply_to_msg_id=77, forum_topic=True)
    forward = Obj(from_id=Obj(channel_id=99), channel_post=123, date=NOW - timedelta(days=3))
    ad = message(
        8,
        age=30,
        sender=2,
        text="服务优惠出售联系 https://one.example",
        edit_date=NOW - timedelta(hours=2),
        fwd_from=forward,
        reply_to=reply,
    )
    other = message(
        7, age=25, sender=3, text="鲜花批发出售联系 https://two.example", reply_to=reply
    )
    warning = message(
        9,
        age=1,
        sender=4,
        text="警告，请删除这一条广告",
        reply_to=Obj(reply_to_top_id=77, reply_to_msg_id=8),
    )
    result = await EvidenceCollector(ReadOnlyClient([warning, ad, other], admins={4})).collect(
        ENTITY, now=NOW
    )
    found = next(item for item in result["evidence"] if item.get("message_id") == 8)
    assert found["date"] == (NOW - timedelta(hours=30)).isoformat()
    assert found["age_hours"] == 2
    assert found["topic_id"] == 77
    assert found["forward_origin"]["peer_id"] == 99
    assert found["warning_reply_ids"] == [9]
    assert result["trial_history_sufficient"]
    assert not result["root_ad_history_sufficient"]
    assert not any(
        item["source"] == "admin_rule" and item.get("message_id") == 9
        for item in result["evidence"]
    )


def advert(sender, text, *, topic=None, **extra):
    return dict(
        source="recent_promotional_message",
        sender_id=sender,
        sender_role="ordinary",
        text=text,
        age_hours=25,
        message_id=sender * 10 if isinstance(sender, int) else None,
        accessible=True,
        warning_search_complete=True,
        topic_id=topic,
        **extra,
    )


def test_one_retained_ad_can_supply_trial_even_with_shared_contact():
    first = advert(1, "优惠服务联系 @SharedShop https://one.example")
    second = advert(2, "促销家具联系 https://t.me/sharedshop https://two.example")
    assert promotion_targets(first["text"]) & promotion_targets(second["text"])
    assert trial_evidence([first, second])


def test_ads_in_different_topics_do_not_prove_root_permission():
    assert not trial_evidence(
        [item for item in [
            advert(1, "优惠服务联系 https://one.example", topic=10),
            advert(2, "优惠服务联系 https://two.example", topic=20),
        ] if item.get("topic_id") is None]
    )


def test_sender_identity_must_be_a_real_positive_id():
    assert not trial_evidence(
        [
            advert(None, "优惠服务联系 https://one.example"),
        ]
    )


@pytest.mark.asyncio
async def test_unknown_full_count_cannot_use_stale_entity_count_to_exit():
    entity = Obj(id=42, megagroup=True, participants_count=2)
    result = await EvidenceCollector(ReadOnlyClient(count=None)).collect(entity, now=NOW)
    assert result["member_count"] is None
    assert result["cached_member_count"] == 2
    assert result["quality_status"] == "qualified"


def test_shared_contacts_do_not_invalidate_a_single_retained_ad():
    assert trial_evidence(
        [
            advert(1, "优惠服务联系 @shared_one"),
            advert(2, "优惠服务联系 @shared_one @shared_two"),
            advert(3, "优惠服务联系 @shared_two"),
        ]
    )


@pytest.mark.asyncio
async def test_mid_page_errors_still_consume_the_global_history_budget():
    class PartialFailureClient(ReadOnlyClient):
        async def iter_messages(self, entity, **kwargs):
            if "filter" in kwargs:
                return
            end = kwargs["offset_date"].replace(tzinfo=None)
            if end > NOW - timedelta(hours=1):
                for index in range(99):
                    yield message(3000 - index, age=index / 100)
                raise TimeoutError()
            async for item in super().iter_messages(entity, **kwargs):
                yield item

    history = [message(2000 - index, age=25 + index / 100) for index in range(1100)]
    result = await EvidenceCollector(PartialFailureClient(history)).collect(ENTITY, now=NOW)
    assert result["sample_count"] == 1000
    assert result["coverage"][0]["sample_count"] == 99
    assert result["coverage"][0]["unknown_reason"] == "TimeoutError"
    assert result["quality_status"] == "technical_wait"


@pytest.mark.asyncio
async def test_missing_permission_response_does_not_manufacture_membership():
    class MissingPermissions(ReadOnlyClient):
        async def get_permissions(self, entity, user):
            return None

    result = await EvidenceCollector(MissingPermissions([message(1)])).collect(ENTITY, now=NOW)
    assert result["permissions"]["member"] is None
    assert result["permissions"]["can_send_text"] is None
    assert result["roles"]["1"] == "unknown"
    assert result["quality_status"] == "technical_wait"


@pytest.mark.asyncio
async def test_many_cached_bots_cannot_hide_actual_ordinary_pair_or_consume_permissions():
    bots = [
        message(
            4000 - index,
            age=25 + index / 1000,
            sender=1000 + index,
            bot=True,
            text=f"优惠服务出售欢迎联系 https://bot-{index}.example",
        )
        for index in range(180)
    ]
    history = bots + [
        message(1000, age=30, sender=2, text="家具特价出售欢迎联系 https://furniture.example"),
        message(5000, age=2, sender=3, text="鲜花批发出售欢迎联系 https://flowers.example"),
        message(5001, age=1, sender=4),
        message(5002, age=0.5, sender=5),
        message(5003, age=0.1, sender=6),
    ]
    client = ReadOnlyClient(history)
    result = await EvidenceCollector(client).collect(ENTITY, now=NOW)
    assert result["permission_queries"] == 5
    assert result["sender_resolution_queries"] == 0
    assert len(client.permission_calls) == 6  # Self plus five actual humans.
    assert result["advertising_candidates_by_role"] == {"bot": 180, "ordinary": 2}
    ordinary = [
        item
        for item in result["evidence"]
        if item.get("sender_role") == "ordinary" and item["source"] == "recent_promotional_message"
    ]
    assert {item["sender_id"] for item in ordinary} == {2, 3}
    assert result["advertising_retained_count"] == 100
    assert result["trial_history_sufficient"]
    assert result["quality_status"] == "qualified"
    assert result["sample_count"] <= 1000
    assert "advertising_evidence_limit" in result["unknowns"]


@pytest.mark.asyncio
async def test_uncached_bot_resolution_stays_bounded_and_does_not_use_human_budget():
    client = ReadOnlyClient()
    resolutions = []

    async def resolve(identity):
        resolutions.append(identity)
        return Obj(id=identity, bot=True)

    client.get_entity = resolve
    collector = EvidenceCollector(client)
    for index in range(80):
        item = message(index, sender=1000 + index)
        item.sender = None
        role = await collector.role(ENTITY, item)
        assert role == ("bot" if index < 60 else "unknown")
    assert len(resolutions) == collector.sender_resolution_queries == 60
    assert collector.permission_queries == 0
    assert await collector.role(ENTITY, message(90, sender=2)) == "ordinary"
    assert collector.permission_queries == 1
    assert collector.role_errors[1079] == "sender_resolution_budget_exhausted"


@pytest.mark.asyncio
async def test_participant_permission_queries_remain_bounded_after_cached_bot_expansion():
    client = ReadOnlyClient()
    collector = EvidenceCollector(client)
    for index in range(120):
        assert await collector.role(ENTITY, message(index, sender=1000 + index, bot=True)) == "bot"
    for index in range(70):
        role = await collector.role(ENTITY, message(index, sender=1 + index))
        assert role == ("ordinary" if index < 60 else "unknown")
    assert collector.permission_queries == len(client.permission_calls) == 60
    assert collector.sender_resolution_queries == 0
    assert collector.role_errors[70] == "identity_budget_exhausted"


def advertising_item(number, sender, url, *, age=30, role="ordinary", warning=()):
    return {
        "source": "recent_promotional_message",
        "message_id": number,
        "text": "优惠出售服务联系 " + url,
        "sender_id": sender,
        "sender_role": role,
        "age_hours": age,
        "topic_id": None,
        "promotion_targets": sorted(promotion_targets(url)),
        "warning_reply_ids": list(warning),
        "accessible": True,
        "warning_search_complete": True,
    }


def test_dedup_before_cap_preserves_a_different_sender_pair():
    from app.modules.acquisition.group_qualification import select_advertising_evidence

    ads = [advertising_item(index, 1, "https://same.example") for index in range(200)]
    ads.append(advertising_item(201, 2, "https://different.example", age=1))
    selected, total = select_advertising_evidence(ads)
    assert len(selected) == total == 2
    assert not trial_evidence(selected)


def test_dedup_selects_pair_before_choosing_same_campaign_representative():
    from app.modules.acquisition.group_qualification import select_advertising_evidence

    ads = [
        advertising_item(1, 1, "https://shared.example", age=40),
        advertising_item(2, 2, "https://shared.example", age=30),
        advertising_item(3, 3, "https://independent.example", age=30),
    ]
    selected, total = select_advertising_evidence(ads)
    assert len(selected) == total == 2
    assert {item["message_id"] for item in selected} == {1, 3}
    assert trial_evidence(selected)


def test_omitted_bot_bridge_does_not_make_linked_promotions_independent():
    from app.modules.acquisition.group_qualification import select_advertising_evidence

    ads = [
        advertising_item(1, 1, "https://one.example"),
        advertising_item(2, 2, "https://two.example"),
        advertising_item(3, 3, "https://one.example https://two.example", role="bot"),
    ]
    selected, _ = select_advertising_evidence(ads)
    assert len({item["association_key"] for item in selected}) == 1
    assert trial_evidence(selected)


@pytest.mark.asyncio
async def test_ad_cap_never_drops_admin_ban_or_warning_target_context():
    ads = [
        message(
            2000 - index,
            age=30 + index / 1000,
            sender=2,
            text=f"优惠出售服务欢迎联系 https://ordinary-{index}.example",
        )
        for index in range(110)
    ]
    warned = message(
        600, age=32, sender=3, bot=True, text="优惠出售服务欢迎联系 https://bot-offer.example"
    )
    warning = message(
        5000, sender=4, text="警告，请删除这条广告", reply_to=Obj(reply_to_msg_id=600)
    )
    rule = message(5001, sender=4, text="本群禁止广告和引流")
    result = await EvidenceCollector(
        ReadOnlyClient(ads + [warned, warning, rule], admins={4})
    ).collect(ENTITY, now=NOW)
    assert any(
        item["source"] == "admin_rule" and item["message_id"] == 5001 for item in result["evidence"]
    )
    assert any(
        item["source"] == "moderation_feedback" and item["message_id"] == 5000
        for item in result["evidence"]
    )
    context = next(item for item in result["evidence"] if item.get("message_id") == 600)
    assert context["source"] == "warned_promotional_message"
    assert context["warning_reply_ids"] == [5000]
    assert result["advertising_retained_count"] == 100


@pytest.mark.asyncio
async def test_unknown_advertiser_cannot_turn_absent_proof_into_exit():
    from app.modules.acquisition.qualification_service import review_schedule

    history = [message(10 + index, sender=1) for index in range(5)] + [
        message(1, age=30, sender=999, text="低价服务优惠出售联系 https://unknown.example")
    ]
    client = ReadOnlyClient(history)
    original = client.get_permissions

    async def permissions(target, user):
        if user != "me" and user.id == 999:
            raise RuntimeError("participant identity inaccessible")
        return await original(target, user)

    client.get_permissions = permissions
    result = await EvidenceCollector(client).collect(ENTITY, now=NOW)
    assert result["history_complete"] and result["valid_messages"] == 5
    assert result["quality_status"] == "qualified"
    assert "sender_identity_unverified" in result["unknowns"]
    assert result["advertising_candidates_by_role"]["unknown"] == 1
    assert not result["trial_history_sufficient"]
    verdict, _, state, _ = review_schedule(
        {**result, "decision": "observe", "reason": "advertising_evidence_insufficient"},
        {"observation_started_at": (NOW - timedelta(hours=25)).isoformat()},
        NOW,
    )
    assert (verdict, state) == ("observe", "completed")


@pytest.mark.asyncio
async def test_unknown_rule_sender_is_retained_and_never_assumed_non_admin():
    client = ReadOnlyClient([message(1, sender=999, text="本群禁止广告和推广")])
    client.role_exception = RuntimeError("identity unavailable")
    result = await EvidenceCollector(client).collect(ENTITY, now=NOW)
    unknown_rule = next(
        item for item in result["evidence"] if item["source"] == "unverified_rule_message"
    )
    assert unknown_rule["sender_role"] == "unknown"
    assert unknown_rule["text"] == "本群禁止广告和推广"
    assert result["rules_incomplete"]
    assert result["quality_status"] == "observe"
    assert "rule_sender_unverified" in result["unknowns"]


@pytest.mark.asyncio
async def test_confirmed_nonmember_stops_sampling_without_rejecting_group():
    class NonMemberClient(ReadOnlyClient):
        async def get_permissions(self, entity, user):
            if user == "me":
                raise UserNotParticipantError(request=None)
            return await super().get_permissions(entity, user)

    client = NonMemberClient([message(number) for number in range(1, 6)], count=30)
    result = await EvidenceCollector(client).collect(ENTITY, now=NOW)
    assert result["quality_status"] == "technical_wait"
    assert result["quality_reason"] == "membership_not_participant"
    assert result["permissions"]["membership_evidence"] == "telegram_permissions_not_participant"
    assert result["permissions"]["member"] is False
    assert not result["technical_errors"]
    assert all(kind != "messages" for kind, _ in client.calls)


@pytest.mark.asyncio
async def test_online_count_falls_back_to_telegram_read_only_request():
    class OnlineFallback(ReadOnlyClient):
        async def __call__(self, request):
            if type(request).__name__ == "GetOnlinesRequest":
                self.calls.append(("onlines", type(request).__name__))
                return Obj(onlines=3)
            return await super().__call__(request)

    client = OnlineFallback()
    client.full.online_count = None
    result = await EvidenceCollector(client).collect(ENTITY, now=NOW)
    assert result["online_count"] == 3
    assert result["online_count_source"] == "messages.getOnlines"
    assert result["online_count_checked_at"] == NOW.isoformat()
    assert ("onlines", "GetOnlinesRequest") in client.calls


@pytest.mark.asyncio
async def test_online_count_unavailable_remains_unknown():
    client = ReadOnlyClient()
    client.full.online_count = None
    result = await EvidenceCollector(client).collect(ENTITY, now=NOW)
    assert result["online_count"] is None
    assert "online_count_unavailable" in result["unknowns"]

class ReplyThreadClient(ReadOnlyClient):
    def __init__(self, history=(), *, reply_threads=None, refreshed=None):
        super().__init__(history)
        self.reply_threads = reply_threads or {}
        self.refreshed = refreshed or {}

    async def get_messages(self, entity, ids):
        self.calls.append(("get_messages", ids))
        return self.refreshed.get(ids) or next(
            (item for item in self.history if item.id == ids), None
        )

    async def iter_messages(self, entity, **kwargs):
        if "reply_to" in kwargs:
            self.calls.append(("reply_thread", kwargs["reply_to"]))
            for item in self.reply_threads.get(kwargs["reply_to"], [])[: kwargs["limit"]]:
                yield item
            return
        async for item in super().iter_messages(entity, **kwargs):
            yield item


def busy_history_with_two_old_ads():
    recent = [message(3000 - index, age=index / 100, sender=1) for index in range(1500)]
    first = message(
        500, age=30, sender=2,
        text="特价家具出售欢迎联系 https://furniture.example",
        replies=Obj(replies=0),
    )
    second = message(
        200, age=54, sender=3,
        text="鲜花批发优惠联系 https://flowers.example",
        replies=Obj(replies=0),
    )
    return recent, first, second


@pytest.mark.asyncio
async def test_complete_ad_reply_threads_support_trial_when_general_history_is_capped():
    recent, first, second = busy_history_with_two_old_ads()
    client = ReplyThreadClient(recent + [first, second])
    result = await EvidenceCollector(client).collect(ENTITY, now=NOW)
    assert result["sample_count"] == 1000 and not result["history_complete"]
    assert result["trial_history_sufficient"]
    proof = [item for item in result["evidence"] if item.get("warning_search_complete")]
    assert {item["message_id"] for item in proof} == {first.id, second.id}
    assert all(item["warning_search_scope"] == "complete_reply_thread" for item in proof)
    assert result["targeted_warning_checked_count"] == 2


@pytest.mark.asyncio
async def test_warning_in_ad_reply_thread_blocks_trial_despite_incomplete_general_history():
    recent, first, second = busy_history_with_two_old_ads()
    second.edit_date = NOW - timedelta(hours=1)
    first.replies = Obj(replies=1)
    warning = message(
        4000, age=1, sender=4, text="警告，请删除这条广告",
        reply_to=Obj(reply_to_msg_id=first.id),
    )
    client = ReplyThreadClient(
        recent + [first, second], reply_threads={first.id: [warning]}
    )
    result = await EvidenceCollector(client).collect(ENTITY, now=NOW)
    assert not result["trial_history_sufficient"]
    warned = next(item for item in result["evidence"] if item.get("message_id") == first.id)
    assert warned["warning_reply_ids"] == [warning.id]
    assert warned["warning_search_scope"] == "complete_reply_thread"


@pytest.mark.asyncio
async def test_incomplete_ad_reply_thread_keeps_trial_unverified():
    recent, first, second = busy_history_with_two_old_ads()
    second.edit_date = NOW - timedelta(hours=1)
    first.replies = Obj(replies=1)
    client = ReplyThreadClient(recent + [first, second])
    result = await EvidenceCollector(client).collect(ENTITY, now=NOW)
    assert not result["trial_history_sufficient"]
    assert "warning_reply_coverage_incomplete" in result["unknowns"]
    first_ad = next(item for item in result["evidence"] if item.get("message_id") == first.id)
    assert first_ad["warning_search_complete"] is False


@pytest.mark.asyncio
async def test_recent_edit_found_during_reply_check_resets_ad_retention_age():
    recent, first, second = busy_history_with_two_old_ads()
    second.edit_date = NOW - timedelta(hours=1)
    edited = message(
        first.id, age=30, sender=2, text=first.message,
        edit_date=NOW - timedelta(hours=1), replies=Obj(replies=0),
    )
    client = ReplyThreadClient(recent + [first, second], refreshed={first.id: edited})
    result = await EvidenceCollector(client).collect(ENTITY, now=NOW)
    assert not result["trial_history_sufficient"]
    first_ad = next(item for item in result["evidence"] if item.get("message_id") == first.id)
    assert first_ad["age_hours"] == 1
    assert first_ad["warning_search_complete"] is False

@pytest.mark.asyncio
async def test_missing_reply_metadata_does_not_claim_complete_warning_search():
    recent, first, second = busy_history_with_two_old_ads()
    second.edit_date = NOW - timedelta(hours=1)
    first.replies = None
    client = ReplyThreadClient(recent + [first, second])
    result = await EvidenceCollector(client).collect(ENTITY, now=NOW)
    assert not result["trial_history_sufficient"]
    assert "warning_reply_count_unconfirmed" in result["unknowns"]
    assert ("reply_thread", first.id) not in client.calls


@pytest.mark.asyncio
async def test_reply_query_failure_keeps_ad_evidence_unknown():
    recent, first, second = busy_history_with_two_old_ads()

    class FailedThreadClient(ReplyThreadClient):
        async def iter_messages(self, entity, **kwargs):
            if "reply_to" in kwargs:
                raise TimeoutError("read unavailable")
            async for item in super().iter_messages(entity, **kwargs):
                yield item

    result = await EvidenceCollector(
        FailedThreadClient(recent + [first, second])
    ).collect(ENTITY, now=NOW)
    assert not result["trial_history_sufficient"]
    assert "warning_reply_query_unavailable" in result["unknowns"]

@pytest.mark.asyncio
async def test_unknown_member_marketing_word_is_not_mistaken_for_group_rule():
    client = ReadOnlyClient([
        message(1, sender=999, text="广告推广优惠套餐，欢迎联系了解详情")
    ])
    client.role_exception = RuntimeError("identity unavailable")
    result = await EvidenceCollector(client).collect(ENTITY, now=NOW)
    assert not result["rules_incomplete"]
    assert not any(
        item["source"] == "unverified_rule_message" for item in result["evidence"]
    )
    assert "sender_identity_unverified" in result["unknowns"]
    assert result["trial_history_sufficient"] is False


@pytest.mark.asyncio
async def test_unknown_adlike_message_with_rule_directive_remains_blocked():
    client = ReadOnlyClient([
        message(1, sender=999, text="本群广告推广优惠套餐需要先审批，请联系管理员")
    ])
    client.role_exception = RuntimeError("identity unavailable")
    result = await EvidenceCollector(client).collect(ENTITY, now=NOW)
    assert result["rules_incomplete"]
    assert any(
        item["source"] == "unverified_rule_message" for item in result["evidence"]
    )
    assert result["quality_status"] == "observe"


@pytest.mark.asyncio
async def test_rule_sender_gets_only_bounded_extra_identity_queries():
    client = ReadOnlyClient()
    collector = EvidenceCollector(client)
    collector.permission_queries = 60
    assert await collector.role(
        ENTITY, message(1, sender=999, text="本群禁止广告")
    ) == "ordinary"
    assert collector.permission_queries == 61
    assert await collector.role(
        ENTITY, message(2, sender=998, text="广告推广优惠套餐，欢迎联系了解详情")
    ) == "unknown"
    assert collector.permission_queries == 61
    collector.permission_queries = 100
    assert await collector.role(
        ENTITY, message(3, sender=997, text="本群广告需要先审批")
    ) == "unknown"
    assert collector.permission_queries == 100


@pytest.mark.asyncio
async def test_message_sender_resolution_precedes_raw_id_fallback():
    client = ReadOnlyClient()

    async def raw_id_not_available(_sender_id):
        raise RuntimeError("raw identity lookup unavailable")

    client.get_entity = raw_id_not_available
    item = message(1, sender=888, text="本群广告需要先审批")
    item.sender = None

    async def get_sender():
        return Obj(id=888, bot=False, deleted=False)

    item.get_sender = get_sender
    collector = EvidenceCollector(client)
    assert await collector.role(ENTITY, item) == "ordinary"
    assert collector.sender_resolution_queries == 1
    assert collector.permission_queries == 1
