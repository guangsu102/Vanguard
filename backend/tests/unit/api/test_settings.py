from app.api.settings import _public_settings
from app.core.config import settings


def test_public_xboard_settings_always_use_environment_source():
    public = _public_settings(
        {
            "xboard": {
                "enabled": False,
                "apiKey": "legacy-value",
            }
        }
    )

    assert public["xboard"] == {
        "enabled": settings.VANGUARD_INTEGRATION_ENABLED,
        "callbackEnabled": settings.VANGUARD_CALLBACK_ENABLED,
        "protocol": "hmac",
        "source": "environment",
    }
