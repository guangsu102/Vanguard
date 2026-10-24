from datetime import datetime, timedelta
from types import SimpleNamespace

from app.modules.acquisition.ad_warmup import readiness


def _policy():
    return {
        "enabled": True,
        "default_warmup_days": 1,
        "minimum_warmup_days": 1,
        "tiers": {},
        "stages": {
            "observe": {"ad_multiplier": 0.0},
            "normal": {"ad_multiplier": 1.0},
        },
    }


def test_dynamic_ad_warmup_uses_durable_account_start_and_group_deadline():
    now = datetime.utcnow()
    account = SimpleNamespace(
        managed_started_at=now - timedelta(hours=12),
        account_type="promoter",
        asset_tier="unknown",
        status="online",
        risk_level="normal",
    )
    member = SimpleNamespace(
        interaction_started_at=now - timedelta(days=2),
        joined_at=now - timedelta(days=2),
        created_at=now - timedelta(days=2),
        first_ad_allowed_at=now + timedelta(hours=3),
    )
    result = readiness(_policy(), account, member, now)
    assert result.reason == "account_warmup_observe_ad_delivery_blocked"
    assert result.next_allowed_at == now + timedelta(hours=12)


def test_legacy_account_without_managed_start_is_not_reset_to_a_new_warmup():
    now = datetime.utcnow()
    account = SimpleNamespace(
        managed_started_at=None,
        account_type="promoter",
        asset_tier="unknown",
        status="online",
        risk_level="normal",
    )
    member = SimpleNamespace(
        interaction_started_at=now - timedelta(days=2),
        joined_at=now - timedelta(days=2),
        created_at=now - timedelta(days=2),
        first_ad_allowed_at=None,
    )
    assert readiness(_policy(), account, member, now).reason is None


def test_zero_day_policy_allows_first_ad_deadline_immediately():
    now = datetime.utcnow()
    account = SimpleNamespace(
        managed_started_at=now,
        account_type="promoter",
        asset_tier="unknown",
        status="online",
        risk_level="normal",
    )
    member = SimpleNamespace(
        interaction_started_at=now,
        joined_at=now,
        created_at=now,
        first_ad_allowed_at=None,
    )
    policy = {
        **_policy(),
        "default_warmup_days": 0,
        "minimum_warmup_days": 0,
        "tiers": {"unknown": {"warmup_days": 0}},
    }

    result = readiness(policy, account, member, now)

    assert result.reason is None
    assert result.next_allowed_at is None


def test_zero_default_without_tier_override_stays_zero():
    now = datetime.utcnow()
    account = SimpleNamespace(
        managed_started_at=now,
        account_type="promoter",
        asset_tier="unknown",
        status="online",
        risk_level="normal",
    )
    member = SimpleNamespace(
        interaction_started_at=now,
        joined_at=now,
        created_at=now,
        first_ad_allowed_at=None,
    )
    policy = {**_policy(), "default_warmup_days": 0, "minimum_warmup_days": 0}

    assert readiness(policy, account, member, now).reason is None


def test_zero_day_policy_retires_cached_warmup_but_nonzero_policy_keeps_deadline():
    now = datetime.utcnow()
    account = SimpleNamespace(
        managed_started_at=now - timedelta(days=2), account_type="promoter",
        asset_tier="unknown", status="online", risk_level="normal",
    )
    cached_deadline = now + timedelta(days=7)
    member = SimpleNamespace(
        interaction_started_at=now - timedelta(days=2), joined_at=now - timedelta(days=2),
        created_at=now - timedelta(days=2), first_ad_allowed_at=cached_deadline,
    )
    assert readiness(_policy(), account, member, now).next_allowed_at == cached_deadline
    zero = {**_policy(), "minimum_warmup_days": 0, "default_warmup_days": 0}
    assert readiness(zero, account, member, now).reason is None
    assert member.first_ad_allowed_at == cached_deadline
