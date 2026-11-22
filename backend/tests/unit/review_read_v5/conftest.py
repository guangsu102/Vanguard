"""Reuse the backend loop/DB fixtures across synchronous asyncio.run helpers."""
import asyncio

import pytest


@pytest.fixture(autouse=True)
def bind_backend_event_loop(event_loop):
    """asyncio.run restores None; keep the still-live fixture loop bound.

    The existing backend fixture has session scope, so its setup does not run
    again after a synchronous heartbeat test uses asyncio.run. Rebinding that
    same loop avoids a harness failure before pytest can execute the next
    async test. This fixture changes no application behavior or test assertion.
    """
    asyncio.set_event_loop(event_loop)
    yield
    if not event_loop.is_closed():
        asyncio.set_event_loop(event_loop)
