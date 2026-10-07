"""Real assess/collection/JSON parsing/single-review policy flow with no Telegram writes."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest

from app.core.account.models import AccountStatus, TelegramAccount
from app.core.group.models import Group, GroupAccountMembership
from app.core.settings_models import SystemSetting
from app.modules.acquisition import qualification_service as qualification
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.group_qualification import POLICY_VERSION
from app.modules.acquisition.models import AdDeliveryLog, GroupQualificationAudit


def answer(mode="soft_ad_allowed", **overrides):
    return {
        "mode": mode,
        "confidence": 99,
        "explicit_permission": mode == "soft_ad_allowed",
        "direct_posting_without_prior_approval": mode == "soft_ad_allowed",
        "requires_admin_approval": False,
        "observed_soft_ad_tolerance": mode == "soft_ad_trial",
        "low_risk_trial_suitable": mode == "soft_ad_trial",
        "conflict": False,
        "applicable_prohibition": mode == "forbidden",
        "supporting_evidence_indexes": []
        if mode == "forbidden"
        else [1, 2]
        if mode == "soft_ad_trial"
        else [0],
        "opposing_evidence_indexes": [0],
        "opposing_evidence_resolution": "规则0只禁止直接URL，明确允许无URL文字广告及简介引导；当前text_profile符合该例外。",
        "rationale": "已核查当前无URL文字和简介引导形式，支持与反对证据均来自当前群规和普通成员。",
        **overrides,
    }


class PolicyClient:
    def __init__(self, rule, pins=()):
        self.now = datetime.utcnow()
        self.entity = Obj(id=1234567890, megagroup=True, broadcast=False)
        self.full = Obj(participants_count=50, online_count=2, about=rule, hidden_prehistory=False)
        self.pins = [
            self.message(200 + index, text=text, sender=8) for index, text in enumerate(pins)
        ]
        self.history = [
            self.message(20 + index, text=f"普通聊天 {index}") for index in range(5)
        ] + [
            self.message(
                10, age=26, text="优惠家具出售欢迎联系 https://furniture.example", sender=12
            ),
            self.message(1, age=30, text="鲜花批发优惠欢迎联系 https://flowers.example", sender=13),
        ]
        self.send_message = AsyncMock(side_effect=AssertionError("read-only assess"))

    def message(self, identity, *, text, age=0.1, sender=11):
        return Obj(
            id=identity,
            sender_id=sender,
            sender=Obj(id=sender, bot=False),
            message=text,
            date=self.now - timedelta(hours=age),
            edit_date=None,
            media=None,
            action=None,
            reply_to=None,
            fwd_from=None,
        )

    async def get_entity(self, _target):
        return self.entity

    async def get_messages(self, _entity, *, ids):
        return [item for item in self.history + self.pins if item.id in ids]

    async def get_me(self):
        return Obj(id=200)

    async def __call__(self, _request):
        return Obj(full_chat=self.full)

    async def get_permissions(self, _entity, _user):
        return Obj(
            is_admin=False,
            is_creator=False,
            has_left=False,
            is_banned=False,
            participant=Obj(banned_rights=None),
        )

    async def iter_messages(self, _entity, **kwargs):
        source = (
            self.pins
            if "filter" in kwargs
            else sorted(self.history, key=lambda x: x.id, reverse=True)
        )
        if "filter" not in kwargs:
            cutoff = kwargs["offset_date"].replace(tzinfo=None)
            source = [
                item
                for item in source
                if item.date < cutoff and (not kwargs["offset_id"] or item.id < kwargs["offset_id"])
            ]
        for item in source[: kwargs["limit"]]:
            yield item


async def assess(
    db, rule, responses, *, pins=(), prior_failure=False, observation_age_hours=None, assess_runs=1, online_count=2, ad_count=2, member_count=50, ad_age_hours=None
):
    now = datetime.utcnow()
    account = TelegramAccount(
        id=2,
        identifier="scope-2",
        session_name="scope-2",
        status=AccountStatus.ONLINE,
        is_active=True,
        risk_level="normal",
    )
    group = Group(id=40, group_id=1234567890, username="ScopeGroup", title="Scope")
    member = GroupAccountMembership(
        id=60,
        account_id=2,
        group_id=40,
        telegram_group_id=group.group_id,
        status="joined",
        joined_at=now - timedelta(days=4),
        review_status="approved",
        ad_status="active",
    )
    row = GroupQualificationAudit(
        batch_id="scope",
        membership_id=60,
        account_id=2,
        group_id=40,
        policy_version=POLICY_VERSION,
        content_scope="text_profile",
        membership_joined_at=member.joined_at,
    )
    if observation_age_hours is not None:
        row.evidence_json = json.dumps(
            {
                "observation_started_at": (
                    now - timedelta(hours=observation_age_hours)
                ).isoformat(),
            }
        )
    db.add_all(
        [
            account,
            group,
            member,
            row,
            SystemSetting(
                key=qualification.SETTING_KEY,
                value=json.dumps(
                    {"enabled": True, "account_ids": [2, 3, 4], "system_user_ids": [200, 300, 400], "system_account_user_ids": {"2": 200}}
                ),
            ),
        ]
    )
    if prior_failure:
        db.add(
            AdDeliveryLog(
                account_id=2,
                group_id=40,
                telegram_group_id=group.group_id,
                ad_campaign_id=1,
                status="sent",
                survival_status="deleted",
            )
        )
    await db.commit()
    client = PolicyClient(rule, pins=pins)
    if ad_age_hours is not None:
        for message in client.history:
            if message.id in {10, 1}:
                message.date = client.now - timedelta(hours=ad_age_hours)
    client.full.online_count = online_count
    client.full.participants_count = member_count
    client.history = [item for item in client.history if item.id > 10 or item.id in ({10, 1} if ad_count == 2 else {1} if ad_count == 1 else set())]
    pool = Obj(
        add_account_from_db=AsyncMock(),
        acquire_by_id=AsyncMock(return_value=Obj(client=client)),
        release=AsyncMock(),
    )
    runtime = AcquisitionAutomationService(db, account_pool=pool)
    outputs = iter(responses)

    async def generate(*_args, **_kwargs):
        assert pool.release.await_count == pool.acquire_by_id.await_count
        item = next(outputs)
        if isinstance(item, BaseException):
            raise item
        return json.dumps(item) if isinstance(item, dict) else item

    llm = Obj(generate=AsyncMock(side_effect=generate))
    runtime._ad_policy_llm_client = llm
    runtime._record_llm_health = AsyncMock()
    for _ in range(assess_runs):
        result = await qualification.assess(runtime, 2, group, row=row)
        await db.flush()
    client.send_message.assert_not_awaited()
    assert 1 <= pool.release.await_count <= assess_runs
    if llm.generate.await_count:
        for call in llm.generate.await_args_list:
            assert (
                "text advertisement with a profile CTA, no direct URL"
                in call.kwargs["system_prompt"]
            )
    return result, row, llm


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rule",
    [
        "本群仅允许指定广告话题讨论，禁止任何推广和引流。",
        "本群禁止外链和简介引流，任何广告都禁止。",
    ],
)
@pytest.mark.parametrize("prior_failure", [False, True])
async def test_applicable_prohibition_precedes_topic_link_and_delivery_observation(
    test_db, rule, prior_failure
):
    result, row, llm = await assess(
        test_db, rule, [answer("forbidden")], prior_failure=prior_failure, ad_age_hours=1
    )
    assert row.decision == "reject" and not result.should_leave and not result.passed
    data = json.loads(row.evidence_json)
    assert data["group_level_advertising_ban"] is True
    assert data["confirmed_group_bans"]
    assert data["qualification_exit_history"][0]["reason"] == "group_rules_ai_disallow_ads"
    assert llm.generate.await_count == 1  # Conservative denial needs no positive authorization.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,expected", [("soft_ad_allowed", "allowed"), ("soft_ad_trial", "trial")]
)
async def test_single_strict_review_can_allow_url_free_profile_scope_under_link_only_ban(
    test_db, mode, expected
):
    result, row, llm = await assess(
        test_db, "禁止直接链接，但允许普通成员文字广告及简介引导。", [answer(mode), answer(mode)], ad_age_hours=1 if mode == "soft_ad_allowed" else 30
    )
    assert row.decision == expected and result.passed and not result.should_leave
    assert llm.generate.await_count == (1 if mode == "soft_ad_allowed" else 0)
    data = json.loads(row.evidence_json)
    if mode == "soft_ad_allowed":
        reviews = data["advertising_audit"]["ai_reviews"]
        assert len(reviews) == 1 and all(review["confidence"] >= 95 for review in reviews)
    assert not data["group_level_advertising_ban"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "timeout",
        "missing_opposition",
        "missing_resolution",
        "low_second",
        "conflict",
        "missing_scope",
    ],
)
async def test_incomplete_or_failed_link_scope_review_stays_paused(test_db, failure):
    first, second = answer(), answer()
    if failure == "timeout":
        second = TimeoutError("test upstream timeout")
    elif failure == "missing_opposition":
        second["opposing_evidence_indexes"] = []
    elif failure == "missing_resolution":
        second["opposing_evidence_resolution"] = ""
    elif failure == "low_second":
        second["confidence"] = 94
    elif failure == "conflict":
        second["conflict"] = True
    else:
        del second["applicable_prohibition"]
    result, row, llm = await assess(
        test_db, "禁止直接链接，但允许普通成员文字广告及简介引导。", [second], ad_age_hours=1
    )
    assert row.decision == "reject" and not result.passed and not result.should_leave
    assert row.reason.startswith("group_rules_ai_")
    assert llm.generate.await_count == 1


@pytest.mark.asyncio
async def test_each_link_restriction_must_be_cited_even_if_legacy_deny_regex_misses_english(
    test_db,
):
    result, row, _ = await assess(
        test_db,
        "允许广告和简介引导。",
        [answer(opposing_evidence_indexes=[]), answer(opposing_evidence_indexes=[])],
        pins=["no links"], ad_age_hours=1
    )
    # Fail-closed contract: an uncited link restriction can no longer settle
    # for observation; the row rejects with the same unconfirmed-scope reason.
    assert row.decision == "reject" and not result.passed
    assert row.reason == "link_or_profile_cta_scope_unconfirmed"


@pytest.mark.asyncio
async def test_topic_route_stays_paused_even_when_single_review_allows_profile_scope(test_db):
    result, row, llm = await assess(
        test_db,
        "禁止直接链接，但允许普通成员文字广告及简介引导；仅指定话题允许广告。",
        [answer(), answer()], ad_age_hours=1
    )
    assert row.decision == "observe" and not result.passed
    assert row.reason == "topic_route_requires_review"
    assert llm.generate.await_count == 1


@pytest.mark.asyncio
async def test_unconfirmed_link_scope_cannot_become_exit_at_observation_deadline(test_db):
    result, row, _ = await assess(
        test_db,
        "禁止直接链接，但允许普通成员文字广告及简介引导。",
        [TimeoutError("test")],
        observation_age_hours=25, ad_age_hours=1
    )
    assert row.decision == "reject" and row.state == "completed"
    assert not result.should_leave
    assert json.loads(row.evidence_json)["ai_decision"] == "fail"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unavailable", "invalid_evidence", "conflict", "consensus"])
async def test_ai_incomplete_reviews_stop_after_three_without_manufacturing_rejection(
    test_db, failure
):
    first = answer(opposing_evidence_indexes=[], opposing_evidence_resolution="")
    second = {**first}
    if failure == "unavailable":
        responses = [TimeoutError("test") for _ in range(3)]
    else:
        if failure == "invalid_evidence":
            second["supporting_evidence_indexes"] = [999]
        elif failure == "conflict":
            second["conflict"] = True
        else:
            second["mode"] = "unknown"
        responses = [second]
    result, row, llm = await assess(
        test_db,
        "允许普通成员发布文字广告与简介引导。",
        responses,
        assess_runs=3,
        ad_count=1,
        ad_age_hours=1,
    )
    data = json.loads(row.evidence_json)
    assert row.decision == "reject" and not result.should_leave
    assert row.state == "completed" and data["ai_final"]
    assert data["ai_decision"] == "fail" and not data["ai_pending"]
    assert not data["qualification_exit_history"]
    assert llm.generate.await_count == 1



@pytest.mark.asyncio
async def test_recent_ad_evidence_keeps_maturity_retry_after_observation_deadline(test_db):
    verdict = answer(
        "unknown",
        supporting_evidence_indexes=[0],
        opposing_evidence_indexes=[],
        opposing_evidence_resolution="",
        rationale="证据完整，但未发现普通成员广告许可；现有普通广告也不满足可接受方式判断。",
    )
    result, row, _ = await assess(
        test_db, "大家友好交流。", [verdict], observation_age_hours=25,
        ad_count=1, ad_age_hours=1
    )
    data = json.loads(row.evidence_json)
    assert row.decision == "reject" and not result.passed and not result.should_leave
    assert datetime.fromisoformat(data["next_evidence_maturity_at"]) == row.next_retry_at
    assert datetime.utcnow() + timedelta(hours=22) < row.next_retry_at <= datetime.utcnow() + timedelta(hours=24)
    assert data["ai_decision"] == "fail" and not data.get("ai_review_incomplete")


@pytest.mark.asyncio
async def test_unrouted_topic_cannot_become_exit_at_observation_deadline(test_db):
    result, row, _ = await assess(
        test_db,
        "允许普通成员文字广告及简介引导；仅指定话题允许广告。",
        [answer(), answer()],
        observation_age_hours=25, ad_age_hours=1
    )
    assert row.decision == "observe" and row.state == "completed"
    assert not result.should_leave
    assert "topic_route_requires_review" in json.loads(row.evidence_json)["unknowns"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rule",
    [
        "广告仅限指定话题。",
        "广告仅限指定话题，其他地方禁止广告。",
        "Advertisements in designated topic only; no advertising outside that topic.",
    ],
)
async def test_topic_only_ai_denial_is_unsupported_capability_not_group_ban(test_db, rule):
    result, row, _ = await assess(test_db, rule, [answer("forbidden")], observation_age_hours=60, ad_age_hours=1)
    data = json.loads(row.evidence_json)
    assert row.decision == "reject" and row.state == "completed"
    assert row.reason == "topic_route_requires_review" and not result.should_leave
    assert not data["group_level_advertising_ban"] and not data["confirmed_group_bans"]
    assert not data["qualification_exit_history"]


@pytest.mark.asyncio
async def test_independent_profile_ban_in_another_pin_still_rejects_with_topic_rule(test_db):
    result, row, _ = await assess(
        test_db,
        "广告仅限指定话题。",
        [answer("forbidden", opposing_evidence_indexes=[0, 1])],
        pins=["禁止简介引流。"], ad_age_hours=1
    )
    assert row.decision == "reject" and not result.should_leave
    assert json.loads(row.evidence_json)["group_level_advertising_ban"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "online_count,expected_reason",
    [(None, "online_count_unknown"), (1, "no_other_online_member")],
)
async def test_ordinary_precedent_does_not_require_online_threshold(test_db, online_count, expected_reason):
    result, row, _ = await assess(
        test_db,
        "允许普通成员文字广告及简介引导。",
        [answer(), answer()],
        online_count=online_count,
        ad_count=1,
    )
    assert row.decision == "trial" and row.reason == "ordinary_member_ads_verified"
    assert result.passed and not result.should_leave


@pytest.mark.asyncio
async def test_one_retained_ordinary_ad_takes_priority_without_ai(test_db):
    result, row, llm = await assess(
        test_db,
        "大家友好交流。",
        [],
        online_count=2,
        ad_count=1,
    )
    assert row.decision == "trial" and result.passed and not result.should_leave
    assert row.reason == "ordinary_member_ads_verified"
    assert llm.generate.await_count == 0
    data = json.loads(row.evidence_json)
    assert data["advertising_audit"]["decision_source"] == "verified_ordinary_member_precedent"


@pytest.mark.asyncio
async def test_one_retained_ordinary_ad_overrides_plain_group_ad_ban(test_db):
    result, row, llm = await assess(
        test_db, "本群禁止任何广告和推广。", [], online_count=2, ad_count=1
    )
    assert row.decision == "trial" and result.passed and not result.should_leave
    assert row.reason == "ordinary_member_ads_verified"
    assert llm.generate.await_count == 0
    snapshot = json.loads(row.evidence_json)
    assert snapshot["precedent_overrides_group_ad_ban"] is True
    assert snapshot["advertising_audit"]["decision_source"] == "verified_ordinary_member_precedent"


@pytest.mark.asyncio
async def test_plain_group_ad_ban_without_ordinary_precedent_still_rejects(test_db):
    result, row, llm = await assess(
        test_db, "本群禁止任何广告和推广。", [answer("forbidden")], ad_count=0
    )
    assert row.decision == "reject" and result.should_leave
    assert llm.generate.await_count == 1


def test_exit_policy_is_a_or_b_or_c_and_d():
    base = {
        "permissions": {"member": True}, "protected": False,
        "coverage": [{"complete": False}, {"complete": False}],
        "ordinary_advertising_last_48h_count": 1, "unknowns": [],
        "member_count_verified": True, "member_count": 50,
        "rules_incomplete": False, "exit_group_rule_ban": False,
        "online_count": 2, "online_count_source": "full_chat.online_count",
    }
    cases = [
        ({}, None),
        ({"coverage": [{"complete": True}, {"complete": True}],
          "ordinary_advertising_last_48h_count": 0}, "no_ordinary_member_ad_48h"),
        ({"member_count": 49}, "members_below_50"),
        ({"exit_group_rule_ban": True, "online_count": 1},
         "group_ad_ban_and_online_below_2"),
        ({"exit_group_rule_ban": True}, None),
        ({"online_count": 1}, None),
        ({"coverage": [{"complete": True}, {"complete": True}],
          "ordinary_advertising_last_48h_count": 0,
          "unknowns": ["online_count_unavailable"]}, "no_ordinary_member_ad_48h"),
        ({"member_count": 49, "unknowns": ["history_hidden"]}, "members_below_50"),
        ({"exit_group_rule_ban": True, "online_count": 1,
          "unknowns": ["history_hidden"]}, "group_ad_ban_and_online_below_2"),
    ]
    for changes, expected in cases:
        assert qualification.automatic_exit_reason({**base, **changes}) == expected
    for changes in ({"permissions": {"member": False}},
                    {"protected": True},
                    {"account_eligibility": {"can_auto_leave": False}}):
        assert qualification.automatic_exit_reason({**base, "member_count": 49, **changes}) is None


@pytest.mark.asyncio
async def test_b_alone_overrides_ordinary_ad_trial(test_db):
    result, row, _ = await assess(
        test_db, "允许普通成员文字广告。", [],
        ad_count=2, member_count=49, online_count=2,
    )
    assert row.decision == "reject" and row.reason == "members_below_50"
    assert result.should_leave
    evidence = json.loads(row.evidence_json)
    assert evidence["ordinary_advertising_last_48h_count"] == 2
    assert evidence["exit_group_rule_ban"] is False


@pytest.mark.asyncio
async def test_c_and_d_alone_override_ordinary_ad_precedent(test_db):
    result, row, _ = await assess(
        test_db, "本群禁止任何广告和推广。", [],
        ad_count=2, member_count=50, online_count=1,
    )
    assert row.decision == "reject" and row.reason == "group_ad_ban_and_online_below_2"
    assert result.should_leave
    evidence = json.loads(row.evidence_json)
    assert evidence["ordinary_advertising_last_48h_count"] == 2
    assert evidence["exit_group_rule_ban"] is True
    assert evidence["advertising_audit"]["ad_allowed"] is True


@pytest.mark.asyncio
async def test_exit_when_all_three_or_arms_hold(test_db):
    result, row, _ = await assess(
        test_db, "本群禁止任何广告和推广。", [answer("forbidden")],
        ad_count=0, member_count=49, online_count=1,
    )
    assert row.decision == "reject" and result.should_leave
    evidence = json.loads(row.evidence_json)
    assert evidence["ordinary_advertising_last_48h_count"] == 0
    assert evidence["exit_group_rule_ban"] is True
    assert qualification.qualifies_for_automatic_exit(evidence)
    assert qualification.qualifies_for_automatic_exit({
        **evidence,
        "coverage": [{"complete": True}, {"complete": True}, {"complete": False}],
        "unknowns": ["history_coverage_incomplete"],
    })

@pytest.mark.asyncio
async def test_ordinary_ad_at_30_hours_only_disables_a_arm(test_db):
    result, row, _ = await assess(
        test_db, "本群禁止任何广告和推广。", [answer("forbidden")],
        ad_count=1, member_count=49, online_count=1,
    )
    evidence = json.loads(row.evidence_json)
    assert evidence["ordinary_advertising_last_48h_count"] == 1
    assert result.should_leave  # B and C&D still hold.
    assert qualification.automatic_exit_reason(evidence) == "members_below_50"


@pytest.mark.asyncio
async def test_explicit_permission_still_exits_when_a_alone_holds(test_db):
    result, row, llm = await assess(
        test_db,
        "允许普通成员文字广告及简介引导。",
        [answer(), answer()],
        online_count=2,
        ad_count=0,
    )
    assert row.decision == "reject" and row.reason == "no_ordinary_member_ad_48h"
    assert not result.passed and result.should_leave
    assert llm.generate.await_count == 1
