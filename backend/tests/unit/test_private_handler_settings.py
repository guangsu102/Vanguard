import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules.acquisition.private_msg import private_handler as private_handler_module
from app.modules.acquisition.private_msg.private_handler import PrivateHandler


@pytest.mark.asyncio
async def test_handle_message_treats_disabled_auto_reply_as_a_skip(monkeypatch):
    handler = PrivateHandler.__new__(PrivateHandler)
    handler._send_lock = asyncio.Lock()
    handler.db = object()
    handler.logger = MagicMock()
    enabled = AsyncMock(return_value=False)
    monkeypatch.setattr(
        private_handler_module,
        "is_private_messaging_enabled",
        enabled,
    )

    result = await handler.handle_message(
        user_id=123456,
        message_text="hello",
        source="private",
    )

    assert result.success is True
    assert result.error is None
    assert result.action_taken == "auto_reply_disabled"
    enabled.assert_awaited_once_with(handler.db, initiated_by_user=True)
