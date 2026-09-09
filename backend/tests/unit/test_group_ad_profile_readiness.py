from datetime import datetime, timedelta
from types import SimpleNamespace

from app.api.automation import _unknown_group_ad_policy_readiness
from app.core.account.models import AccountOperationMode, AccountStatus
from app.modules.acquisition.models import GroupAdPolicyMode


def _profile(**overrides):
    values = {
        "ad_policy_mode": GroupAdPolicyMode.UNKNOWN.value,
        "ad_policy_probe_status": "not_started",
        "ad_policy_probe_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _membership(now: datetime, **overrides):
    values = {
        "account_id": 1,
        "ad_status": "active",
        "probe_status": "success",
        "first_ad_allowed_at": now - timedelta(minutes=1),
        "ad_eligible_after": now - timedelta(minutes=1),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _account(**overrides):
    values = {
        "is_active": True,
        "status": AccountStatus.ONLINE,
        "risk_level": "normal",
        "risk_pause_until": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _readiness(profile, memberships, now: datetime, **overrides):
    conditions = {
        "active_binding_account_ids": {1},
        "group_active": True,
        "group_can_receive_ads": True,
        "window_reason": None,
    }
    conditions.update(overrides)
    return _unknown_group_ad_policy_readiness(
        profile,
        memberships,
        now,
        **conditions,
    )


def test_unknown_group_policy_reports_ready_probe_candidate():
    now = datetime.utcnow()

    readiness = _readiness(
        _profile(),
        [(_membership(now), _account(), None)],
        now,
    )

    assert readiness == {
        "reason": "ready_for_probe",
        "label": "可探测",
        "ready": True,
    }


def test_unknown_group_policy_reports_warmup_wait():
    now = datetime.utcnow()

    readiness = _readiness(
        _profile(),
        [
            (
                _membership(
                    now,
                    first_ad_allowed_at=now + timedelta(hours=2),
                ),
                _account(),
                None,
            )
        ],
        now,
    )

    assert readiness["reason"] == "waiting_warmup"
    assert readiness["label"] == "等待养号"
    assert readiness["ready"] is False


def test_unknown_group_policy_does_not_use_ad_only_account_for_probe():
    now = datetime.utcnow()
    operation_config = SimpleNamespace(
        operation_mode=AccountOperationMode.AD_ONLY.value
    )

    readiness = _readiness(
        _profile(),
        [(_membership(now), _account(), operation_config)],
        now,
    )

    assert readiness["reason"] == "no_growth_probe_account"
    assert readiness["ready"] is False


def test_unknown_group_policy_reports_unavailable_account():
    now = datetime.utcnow()

    readiness = _readiness(
        _profile(),
        [
            (
                _membership(now),
                _account(status=AccountStatus.BANNED),
                None,
            )
        ],
        now,
    )

    assert readiness["reason"] == "account_unavailable"
    assert readiness["ready"] is False


def test_unknown_group_policy_reports_no_joined_account():
    now = datetime.utcnow()

    readiness = _readiness(_profile(), [], now)

    assert readiness["reason"] == "no_joined_account"
    assert readiness["ready"] is False


def test_unknown_group_policy_reports_write_probe_wait():
    now = datetime.utcnow()

    readiness = _readiness(
        _profile(),
        [(_membership(now, probe_status="scheduled"), _account(), None)],
        now,
    )

    assert readiness["reason"] == "waiting_write_probe"
    assert readiness["label"] == "等待发言能力验证"


def test_unknown_group_policy_reports_ad_eligibility_wait():
    now = datetime.utcnow()

    readiness = _readiness(
        _profile(),
        [
            (
                _membership(now, ad_eligible_after=now + timedelta(hours=2)),
                _account(),
                None,
            )
        ],
        now,
    )

    assert readiness["reason"] == "waiting_ad_eligible"
    assert readiness["label"] == "等待可投放时间"


def test_unknown_group_policy_reports_blocked_membership():
    now = datetime.utcnow()

    readiness = _readiness(
        _profile(),
        [(_membership(now, ad_status="blocked"), _account(), None)],
        now,
    )

    assert readiness["reason"] == "membership_blocked"


def test_unknown_group_policy_requires_active_ad_binding():
    now = datetime.utcnow()

    readiness = _readiness(
        _profile(),
        [(_membership(now), _account(), None)],
        now,
        active_binding_account_ids=set(),
    )

    assert readiness["reason"] == "no_active_ad_binding"
    assert readiness["label"] == "没有有效广告绑定"


def test_unknown_group_policy_respects_account_ad_switches():
    now = datetime.utcnow()
    operation_config = SimpleNamespace(
        operation_mode=AccountOperationMode.GROWTH.value,
        enabled=True,
        auto_ads_enabled=False,
    )

    readiness = _readiness(
        _profile(),
        [(_membership(now), _account(), operation_config)],
        now,
    )

    assert readiness["reason"] == "account_ads_disabled"


def test_unknown_group_policy_reports_account_risk_pause():
    now = datetime.utcnow()

    readiness = _readiness(
        _profile(),
        [
            (
                _membership(now),
                _account(risk_pause_until=now + timedelta(hours=1)),
                None,
            )
        ],
        now,
    )

    assert readiness["reason"] == "account_risk_blocked"
    assert readiness["label"] == "账号风控暂停"
    assert readiness["ready"] is False


def test_unknown_group_policy_respects_group_ad_level():
    now = datetime.utcnow()

    readiness = _readiness(
        _profile(),
        [(_membership(now), _account(), None)],
        now,
        group_can_receive_ads=False,
    )

    assert readiness["reason"] == "group_level_disallows_ads"


def test_unknown_group_policy_waits_for_delivery_window():
    now = datetime.utcnow()

    readiness = _readiness(
        _profile(),
        [(_membership(now), _account(), None)],
        now,
        window_reason="ad_time_window_blocked",
    )

    assert readiness["reason"] == "waiting_delivery_window"
    assert readiness["label"] == "等待投放时间窗"


def test_unknown_group_policy_reports_probe_in_progress():
    now = datetime.utcnow()

    readiness = _readiness(
        _profile(ad_policy_probe_status="sending"),
        [(_membership(now), _account(), None)],
        now,
    )

    assert readiness["reason"] == "probe_in_progress"


def test_unknown_group_policy_waits_for_failed_probe_cooldown():
    now = datetime.utcnow()

    readiness = _readiness(
        _profile(
            ad_policy_probe_status="failed",
            ad_policy_probe_at=now - timedelta(hours=1),
        ),
        [(_membership(now), _account(), None)],
        now,
    )

    assert readiness["reason"] == "waiting_probe_cooldown"
    assert readiness["label"] == "等待探测冷却"
    assert readiness["ready"] is False
