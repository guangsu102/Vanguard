"""
Pytest configuration for Acquisition module tests.
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock


@pytest.fixture
def mock_db():
    """Mock database session."""
    db = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    return db


@pytest.fixture
def mock_account_pool():
    """Mock account pool."""
    pool = AsyncMock()
    pool.acquire = AsyncMock()
    pool.release = AsyncMock()
    return pool


@pytest.fixture
def mock_keyword_engine():
    """Mock keyword engine."""
    engine = MagicMock()
    engine.match = AsyncMock(return_value=[])
    engine.load_keywords = AsyncMock(return_value=0)
    return engine


@pytest.fixture
def mock_group_manager():
    """Mock group manager."""
    manager = AsyncMock()
    manager.get_group_by_id = AsyncMock(return_value=None)
    manager.get_groups_by_level = AsyncMock(return_value=[])
    return manager


@pytest.fixture
def event_loop():
    """Create an instance of the default event loop for each test case."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()
