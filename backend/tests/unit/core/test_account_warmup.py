from datetime import datetime, timedelta
from types import SimpleNamespace

from app.core.account.models import AccountAssetTier, AccountStatus, AccountType
from app.core.account.warmup import account_warmup_block_reason, account_warmup_context
from app.core.automation_settings import normalize_account_warmup_policy_settings


def _account(**kwargs):
    now = datetime.utcnow()
    defaults = {
        "phone": "+15550009000",
        "identifier": "+15550009000",
        "account_type": AccountType.PROMOTER,
        "api_config_name": "default",
        "country_code": "US",
        "session_name": "warmup_session",
        "status": AccountStatus.ONLINE,
        "created_at": now,
        "managed_started_at": now,
        "asset_tier": AccountAssetTier.UNKNOWN.value,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_account_warmup_context_blocks_ads_during_observe_stage():
    policy = normalize_account_warmup_policy_settings(
        {
            "default_warmup_days": 1,
            "minimum_warmup_days": 1,
            "tiers": {"unknown": {"warmup_days": 1}},
        }
    )
    account = _account()

    context = account_warmup_context(policy, account, datetime.utcnow(), action="ad_delivery")

    assert context.stage == "observe"
    assert context.action_multiplier == 0
    assert account_warmup_block_reason(context, "ad_delivery") == "account_warmup_observe_ad_delivery_blocked"


def test_account_warmup_context_enters_normal_after_tier_days():
    now = datetime.utcnow()
    policy = normalize_account_warmup_policy_settings(
        {
            "default_warmup_days": 1,
            "minimum_warmup_days": 1,
            "tiers": {"year_3_plus": {"warmup_days": 1}},
        }
    )
    account = _account(
        asset_tier=AccountAssetTier.YEAR_3_PLUS.value,
        managed_started_at=now - timedelta(days=1),
    )

    before = account_warmup_context(policy, account, now - timedelta(seconds=1), action="ad_delivery")
    assert before.stage == "observe"
    assert before.action_multiplier == 0

    context = account_warmup_context(policy, account, now, action="ad_delivery")

    assert context.stage == "normal"
    assert context.action_multiplier == 1.0
    assert context.remaining_days == 0


def test_zero_day_warmup_enters_normal_immediately():
    now = datetime.utcnow()
    policy = normalize_account_warmup_policy_settings(
        {
            "default_warmup_days": 0,
            "minimum_warmup_days": 0,
            "tiers": {"unknown": {"warmup_days": 0}},
        }
    )
    account = _account(managed_started_at=now)

    context = account_warmup_context(policy, account, now, action="ad_delivery")

    assert context.stage == "normal"
    assert context.warmup_days == 0
    assert context.action_multiplier == 1.0
    assert account_warmup_block_reason(context, "ad_delivery") is None


def test_short_warmup_still_enters_ramp_before_normal():
    now = datetime.utcnow()
    policy = normalize_account_warmup_policy_settings(
        {
            "default_warmup_days": 5,
            "minimum_warmup_days": 5,
            "tiers": {"unknown": {"warmup_days": 5}},
        }
    )
    account = _account(managed_started_at=now - timedelta(days=4))

    context = account_warmup_context(policy, account, now, action="ad_delivery")

    assert context.stage == "ramp"
    assert context.warmup_days == 5
    assert context.remaining_days == 1


def test_user_initiated_private_message_is_not_blocked_by_warmup():
    policy = normalize_account_warmup_policy_settings(
        {
            "default_warmup_days": 1,
            "minimum_warmup_days": 1,
            "tiers": {"unknown": {"warmup_days": 1}},
        }
    )
    account = _account()

    context = account_warmup_context(
        policy,
        account,
        datetime.utcnow(),
        action="private_message",
        details={"initiated_by_user": True},
    )

    assert context.stage == "observe"
    assert context.action_multiplier == 1.0
    assert account_warmup_block_reason(context, "private_message", {"initiated_by_user": True}) is None
