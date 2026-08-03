"""
Pytest Configuration and Fixtures
"""

import asyncio
import importlib
import os
from typing import AsyncGenerator, Generator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.core.database import Base, get_db

os.environ.pop("SSLKEYLOGFILE", None)


# Test database URL
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


def _import_models() -> None:
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
        "app.modules.qq.models",
        "app.integrations.xboard.models",
    ):
        importlib.import_module(model_module)


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
    _import_models()
    
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    
    async with async_session() as session:
        yield session
    
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def client(test_db: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Create test HTTP client."""
    from app.main import app
    
    async def override_get_db():
        yield test_db
    
    app.dependency_overrides[get_db] = override_get_db
    
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test"
    ) as ac:
        yield ac
    
    app.dependency_overrides.clear()
