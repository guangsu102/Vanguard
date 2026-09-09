import pytest

from app.core.config import DEFAULT_DEV_SECRET, Settings


def _production_settings(**overrides):
    secret = "x" * 64
    values = {
        "APP_ENV": "production",
        "DATABASE_URL": "postgresql+asyncpg://vanguard:secure@postgres:5432/vanguard",
        "JWT_SECRET": secret,
        "VANGUARD_SIGNING_SECRET": "shared-secret",
        "VANGUARD_CALLBACK_SIGNING_SECRET": "callback-secret",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_production_still_rejects_missing_secret():
    with pytest.raises(ValueError, match="JWT_SECRET must be set explicitly"):
        _production_settings(JWT_SECRET=DEFAULT_DEV_SECRET)


def test_owned_group_execution_defaults_off_and_kill_switch_is_available():
    configured = Settings(_env_file=None)

    assert configured.OWNED_GROUP_EXECUTION_ENABLED is False
    assert configured.P0_SAFETY_GATE_ENABLED is True
    assert configured.P0_SAFETY_GATE_FAIL_CLOSED is False
    assert configured.OWNED_GROUP_KILL_SWITCH_ENABLED is False


def test_production_owned_group_execution_requires_fail_closed_safety_gate():
    with pytest.raises(ValueError, match="P0_SAFETY_GATE_FAIL_CLOSED"):
        _production_settings(OWNED_GROUP_EXECUTION_ENABLED=True)


def test_production_owned_group_execution_requires_enabled_safety_gate():
    with pytest.raises(ValueError, match="P0_SAFETY_GATE_ENABLED"):
        _production_settings(
            OWNED_GROUP_EXECUTION_ENABLED=True,
            P0_SAFETY_GATE_ENABLED=False,
            P0_SAFETY_GATE_FAIL_CLOSED=True,
        )


def test_production_owned_group_execution_requires_module_enabled():
    with pytest.raises(ValueError, match="OWNED_GROUP_MODULE_ENABLED"):
        _production_settings(
            OWNED_GROUP_EXECUTION_ENABLED=True,
            OWNED_GROUP_MODULE_ENABLED=False,
            P0_SAFETY_GATE_FAIL_CLOSED=True,
        )


def test_production_owned_group_execution_can_start_with_static_kill_switch_engaged():
    configured = _production_settings(
        OWNED_GROUP_EXECUTION_ENABLED=True,
        P0_SAFETY_GATE_ENABLED=True,
        P0_SAFETY_GATE_FAIL_CLOSED=True,
        OWNED_GROUP_KILL_SWITCH_ENABLED=True,
    )

    assert configured.OWNED_GROUP_EXECUTION_ENABLED is True
    assert configured.OWNED_GROUP_KILL_SWITCH_ENABLED is True


def test_onebot_requires_numeric_account_and_strong_token():
    with pytest.raises(ValueError, match="numeric QQ account"):
        Settings(
            _env_file=None,
            QQ_ONEBOT_ENABLED=True,
            QQ_ONEBOT_ACCOUNT_ID="not-a-qq-number",
            QQ_ONEBOT_ACCESS_TOKEN="t" * 32,
        )

    with pytest.raises(ValueError, match="at least 32 characters"):
        Settings(
            _env_file=None,
            QQ_ONEBOT_ENABLED=True,
            QQ_ONEBOT_ACCOUNT_ID="10001",
            QQ_ONEBOT_ACCESS_TOKEN="short",
        )


def test_onebot_accepts_numeric_account_and_strong_token():
    configured = Settings(
        _env_file=None,
        QQ_ONEBOT_ENABLED=True,
        QQ_ONEBOT_ACCOUNT_ID="10001",
        QQ_ONEBOT_ACCESS_TOKEN="t" * 32,
    )

    assert configured.QQ_ONEBOT_ENABLED is True


def test_alert_chat_id_is_loaded_from_canonical_setting():
    configured = Settings(_env_file=None, ALERT_CHAT_ID='-1001234567890')

    assert configured.ALERT_CHAT_ID == '-1001234567890'


def test_fast_llm_model_falls_back_to_primary_model_when_empty():
    configured = Settings(
        _env_file=None,
        LLM_MODEL='primary-model',
        LLM_FAST_MODEL='',
    )

    assert configured.LLM_FAST_MODEL == 'primary-model'


def test_redis_url_password_takes_precedence_over_legacy_password():
    configured = Settings(
        _env_file=None,
        REDIS_URL="redis://:url-secret@redis.example:6379/0",
        REDIS_PASSWORD="legacy-secret",
    )

    assert configured.effective_redis_password is None


def test_redis_password_is_used_when_url_has_no_password():
    configured = Settings(
        _env_file=None,
        REDIS_URL="redis://redis.example:6379/0",
        REDIS_PASSWORD="legacy-secret",
    )

    assert configured.effective_redis_password == "legacy-secret"
