import pytest

from app.core.config import DEFAULT_DEV_SECRET, Settings


def _production_settings(**overrides):
    secret = "x" * 64
    values = {
        "APP_ENV": "production",
        "DATABASE_URL": "postgresql+asyncpg://vanguard:secure@postgres:5432/vanguard",
        "JWT_SECRET": secret,
        "SECRET_KEY": secret,
        "VANGUARD_SIGNING_SECRET": "shared-secret",
        "VANGUARD_CALLBACK_SIGNING_SECRET": "callback-secret",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_secret_key_falls_back_to_jwt_secret_in_production():
    secret = "j" * 64

    settings = _production_settings(JWT_SECRET=secret, SECRET_KEY=DEFAULT_DEV_SECRET)

    assert settings.SECRET_KEY == secret
    assert settings.JWT_SECRET == secret


def test_jwt_secret_falls_back_to_secret_key_in_production():
    secret = "s" * 64

    settings = _production_settings(SECRET_KEY=secret, JWT_SECRET=DEFAULT_DEV_SECRET)

    assert settings.SECRET_KEY == secret
    assert settings.JWT_SECRET == secret


def test_production_still_rejects_missing_secret():
    with pytest.raises(ValueError, match="SECRET_KEY must be set explicitly"):
        _production_settings(SECRET_KEY=DEFAULT_DEV_SECRET, JWT_SECRET=DEFAULT_DEV_SECRET)


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


def test_legacy_telegram_alert_chat_id_alias_is_normalized():
    configured = Settings(_env_file=None, TELEGRAM_ALERT_CHAT_ID='-1001234567890')

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
