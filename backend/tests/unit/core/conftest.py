"""
Pytest Configuration and Fixtures for Keyword Engine Tests
"""

import asyncio
import importlib
import sys
from typing import AsyncGenerator, Generator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

# Add backend to path
sys.path.insert(0, "d:/tanxuan/project/Vanguard/backend")

from app.core.database import Base


# Test database URL
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """Create event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="function")
async def test_db() -> AsyncGenerator[AsyncSession, None]:
    """Create test database session."""
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    # Register the complete metadata dependency graph. This nested conftest may
    # be collected by itself, so it cannot rely on unrelated tests importing
    # foreign-key targets first.
    for model_module in (
        "app.core.account.models",
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
    
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    
    async with async_session() as session:
        yield session
    
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    
    await engine.dispose()
