"""Read-only pre-join hints must not grant ad permission or bypass rate limits."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.modules.acquisition import candidate_preview
from app.modules.acquisition.automation import (
    AcquisitionAutomationService,
    AutomationRunResult,
)
from app.modules.acquisition.candidate_preview import (
    CandidatePreview,
    independent_ads,
    public_candidate_preview,
)
from app.modules.acquisition.search.group_finder import TelegramFloodWaitError


class FakeClient:
    def __init__(self, *, about="", messages=(), pins=(), entity_id=123):
        self.entity = SimpleNamespace(id=entity_id, megagroup=True)
        self.about = about
        self.messages = messages
        self.pins = pins
        self.calls = []

    async def get_entity(self, username):
        self.calls.append(("entity", username))
        return self.entity

    async def __call__(self, request):
        self.calls.append(("full", type(request).__name__))
        return SimpleNamespace(full_chat=SimpleNamespace(about=self.about))

    def iter_messages(self, entity, **kwargs):
        self.calls.append(("history", kwargs))

        async def items():
            source = self.pins if kwargs.get("filter") else self.messages
            for item in source:
                yield item

        return items()


def message(sender_id, target, now):
    return SimpleNamespace(
        sender_id=sender_id,
        message=f"优惠套餐，请联系 @{target}",
        date=now - timedelta(hours=30),
        edit_date=None,
        action=None,
        reply_to=None,
        sender=SimpleNamespace(bot=False),
        media=None,
    )


@pytest.mark.asyncio
async def test_preview_only_reads_identity_and_metadata_not_pins_history_or_senders(monkeypatch):
    now = datetime.now(UTC)
    client = FakeClient(
        about="允许广告推广",
        messages=[message(21, "seller_one", now), message(22, "seller_two", now)],
    )

    result = await public_candidate_preview(
        client,
        SimpleNamespace(username="a", group_id=-1000000000123),
        own_user_ids={99},
        identity_coverage=True,
        now=now,
    )
    assert result.status == "metadata_checked"
    assert result.rules_readable
    assert result.rule_signal == "explicit_allow"
    assert result.ordinary_advertisers == 0
    assert not result.independent_ads
    assert result.score == 5
    assert {name for name, _ in client.calls} == {"entity", "full"}


@pytest.mark.asyncio
async def test_preview_without_complete_own_account_coverage_never_counts_ads():
    now = datetime.now(UTC)
    client = FakeClient(
        about="禁止广告推广",
        messages=[message(21, "seller_one", now), message(22, "seller_two", now)],
    )
    result = await public_candidate_preview(
        client,
        SimpleNamespace(username="a", group_id=-1000000000123),
        own_user_ids=set(),
        identity_coverage=False,
        now=now,
    )
    assert result.status == "metadata_checked"
    assert result.rule_signal == "explicit_ban"
    assert result.ordinary_advertisers == 0
    assert not result.independent_ads
    assert sum(name == "history" for name, _ in client.calls) == 0


@pytest.mark.asyncio
async def test_english_ad_rule_is_readable():
    result = await public_candidate_preview(
        FakeClient(about="Ads are allowed"),
        SimpleNamespace(username="a", group_id=-1000000000123),
        own_user_ids=set(),
        identity_coverage=False,
    )
    assert result.rules_readable
    assert result.rule_signal == "explicit_allow"


@pytest.mark.asyncio
async def test_empty_public_description_is_not_readable_group_rule():
    result = await public_candidate_preview(
        FakeClient(),
        SimpleNamespace(username="a", group_id=-1000000000123),
        own_user_ids=set(),
        identity_coverage=False,
    )
    assert not result.rules_readable
    assert result.score == 0


@pytest.mark.asyncio
async def test_preview_excludes_broadcast_channel_before_join():
    client = FakeClient()
    client.entity.broadcast = True
    client.entity.megagroup = False
    result = await public_candidate_preview(
        client,
        SimpleNamespace(username="a", group_id=-1000000000123),
        own_user_ids=set(),
        identity_coverage=False,
    )
    assert result.status == "not_joinable"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_preview_excludes_mismatched_username():
    client = FakeClient(entity_id=456)
    result = await public_candidate_preview(
        client,
        SimpleNamespace(username="a", group_id=-1000000000123),
        own_user_ids=set(),
        identity_coverage=True,
    )
    assert result.status == "identity_mismatch"
    assert len(client.calls) == 1


def test_one_mature_ordinary_ad_takes_priority_over_restriction():
    preview = CandidatePreview(
        "sampled",
        rules_readable=True,
        rule_signal="restriction",
        ordinary_advertisers=2,
        independent_ads=True,
    )
    assert preview.score > CandidatePreview(status="sampled", rule_signal="explicit_allow").score


def test_campaign_bridge_is_one_advertiser_campaign():
    assert not independent_ads(
        [
            (21, {"a"}),
            (22, {"a", "b"}),
            (23, {"b"}),
        ]
    )
    assert independent_ads([(21, {"a"}), (22, {"b"})])


@pytest.mark.asyncio
async def test_negated_ban_is_not_a_restriction():
    client = FakeClient(about="不禁止广告")
    result = await public_candidate_preview(
        client,
        SimpleNamespace(username="a", group_id=-1000000000123),
        own_user_ids=set(),
        identity_coverage=False,
    )
    assert result.rule_signal == "no_clear_signal"


@pytest.mark.asyncio
async def test_preview_propagates_flood_wait():
    class FloodClient(FakeClient):
        async def get_entity(self, username):
            raise RuntimeError("A wait of 40 seconds is required")

    with pytest.raises(TelegramFloodWaitError) as exc:
        await public_candidate_preview(
            FloodClient(),
            SimpleNamespace(username="a", group_id=-1000000000123),
            own_user_ids=set(),
            identity_coverage=False,
        )
    assert exc.value.seconds == 40


@pytest.mark.asyncio
async def test_ranker_prefers_v3_hints_and_excludes_identity_mismatch(monkeypatch, test_db):
    from app.modules.acquisition import qualification_service, qualification_system_identity

    monkeypatch.setattr(
        qualification_service,
        "policy",
        AsyncMock(return_value={"enabled": True, "system_account_user_ids": {"1": 7}}),
    )
    monkeypatch.setattr(
        qualification_system_identity,
        "covered_system_ids",
        AsyncMock(return_value=({7}, "fingerprint")),
    )
    hints = {
        1: CandidatePreview("sampled", rules_readable=True, rule_signal="restriction", peer_namespace="channel", member_count=100),
        2: CandidatePreview("sampled", rules_readable=True, rule_signal="explicit_allow", peer_namespace="channel", member_count=100),
        3: CandidatePreview("identity_mismatch"),
    }
    monkeypatch.setattr(
        "app.modules.acquisition.automation.public_candidate_preview",
        AsyncMock(side_effect=lambda _client, group, **_kw: hints[group.id]),
    )
    client = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=7)))
    pool = SimpleNamespace(
        acquire_by_id=AsyncMock(return_value=SimpleNamespace(client=client)),
        release=AsyncMock(),
    )
    service = object.__new__(AcquisitionAutomationService)
    service.db = test_db
    service.account_pool = pool
    service.logger = Mock()
    groups = [
        SimpleNamespace(id=1, group_id=-1000000000101, username="a"),
        SimpleNamespace(id=2, group_id=-1000000000102, username="b"),
        SimpleNamespace(id=3, group_id=-1000000000103, username="c"),
    ]
    ordered = await service._rank_pending_join_candidates(1, groups)
    assert [group.id for group in ordered] == [2, 1]
    pool.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_ranker_disabled_does_not_read_telegram(monkeypatch):
    from app.modules.acquisition import qualification_service

    monkeypatch.setattr(
        qualification_service, "policy", AsyncMock(return_value={"enabled": False})
    )
    pool = SimpleNamespace(acquire_by_id=AsyncMock())
    service = object.__new__(AcquisitionAutomationService)
    service.db = object()
    service.account_pool = pool
    groups = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
    assert await service._rank_pending_join_candidates(1, groups) == groups
    pool.acquire_by_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_join_preview_flood_wait_cools_account_before_join():
    service = object.__new__(AcquisitionAutomationService)
    service._maybe_run_periodic_group_cleanup = AsyncMock(return_value=None)
    service._sync_account_business_stage = AsyncMock()
    service._check_join_quota = AsyncMock(return_value=None)
    service._next_pending_join_group = AsyncMock(
        side_effect=TelegramFloodWaitError(
            40, operation="join_candidate_preview", original=RuntimeError("flood")
        )
    )
    resume_at = datetime.utcnow() + timedelta(minutes=2)
    service._apply_account_flood_wait = AsyncMock(return_value=resume_at)
    service._attempt_join_queued_group = AsyncMock()
    account = SimpleNamespace(id=1)
    op_config = SimpleNamespace(account=account, account_id=1, next_join_after=None)
    result = await service._run_auto_join_for_account_config(
        op_config,
        now=datetime.utcnow(),
        keywords_per_account=1,
        max_groups_per_keyword=1,
        dry_run=False,
    )
    assert isinstance(result, AutomationRunResult)
    assert result.skipped == 1
    assert result.details[-1]["action"] == "account_cooling_down"
    service._attempt_join_queued_group.assert_not_awaited()
    service._apply_account_flood_wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_queue_reserves_v3_ranked_candidate_before_legacy_first():
    groups = [
        SimpleNamespace(id=1, group_id=101),
        SimpleNamespace(id=2, group_id=102),
    ]
    rows = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: groups)
    )
    service = object.__new__(AcquisitionAutomationService)
    service.db = SimpleNamespace(execute=AsyncMock(return_value=rows))
    service._joined_membership_account_id_for_group = AsyncMock(return_value=None)
    service._rank_pending_join_candidates = AsyncMock(return_value=[groups[1], groups[0]])
    service._reserve_auto_join_candidate = AsyncMock(return_value=True)
    selected = await service._next_pending_join_group(account_id=7, preview=True)
    assert selected is groups[1]
    service._rank_pending_join_candidates.assert_awaited_once_with(7, groups)
    service._reserve_auto_join_candidate.assert_awaited_once_with(102, None, None)


@pytest.mark.asyncio
async def test_queue_dry_run_keeps_legacy_order_and_skips_preview():
    groups = [
        SimpleNamespace(id=1, group_id=101),
        SimpleNamespace(id=2, group_id=102),
    ]
    rows = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: groups)
    )
    service = object.__new__(AcquisitionAutomationService)
    service.db = SimpleNamespace(execute=AsyncMock(return_value=rows))
    service._joined_membership_account_id_for_group = AsyncMock(return_value=None)
    service._rank_pending_join_candidates = AsyncMock()
    service._reserve_auto_join_candidate = AsyncMock(return_value=True)
    selected = await service._next_pending_join_group(account_id=7, preview=False)
    assert selected is groups[0]
    service._rank_pending_join_candidates.assert_not_awaited()
