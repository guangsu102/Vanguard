"""Register the pure ORM models required by telemetry's SQLite fixtures."""
import importlib

import pytest


@pytest.fixture(scope="session", autouse=True)
def register_pure_models() -> None:
    # Match the join-floor model registry. This imports definitions only and
    # does not load app.main, P2, create tables, or start application services.
    for module in (
        "app.core.account.models",
        "app.core.account.control_models",
        "app.core.group.models",
        "app.core.keyword.models",
        "app.core.user.models",
        "app.core.campaign.models",
        "app.core.worker_status",
        "app.core.settings_models",
        "app.modules.guardian.models",
        "app.modules.acquisition.models",
        "app.modules.private_chat.models",
        "app.modules.qq.models",
        "app.modules.owned_group.models",
        "app.modules.owned_group.models_extra",
        "app.modules.owned_group.messaging_models",
        "app.integrations.xboard.models",
    ):
        importlib.import_module(module)
