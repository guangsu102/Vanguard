"""Pure model registry for the join-floor SQLite regressions."""
import importlib
from typing import AsyncGenerator
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession,create_async_engine,async_sessionmaker
from app.core.database import Base

TEST_DATABASE_URL="sqlite+aiosqlite:///:memory:"

def _import_models() -> None:
    for model_module in (
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
        importlib.import_module(model_module)

@pytest_asyncio.fixture(scope="function")
async def test_db() -> AsyncGenerator[AsyncSession, None]:
    """Create test database session."""
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    _import_models()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    await engine.dispose()
