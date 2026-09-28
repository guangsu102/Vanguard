from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.modules.guardian.verification.verification_mgr import VerificationManager


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("username", "expected"),
    [(None, r"User\_702"), ("first_last", r"first\_last"),
     ("plainname", "plainname"), ("a*b`c[d", r"a\*b\`c\[d")],
)
async def test_welcome_escapes_dynamic_name_but_preserves_template(username, expected):
    manager = VerificationManager(db=None)
    manager.should_verify = AsyncMock(return_value=(False, "disabled"))
    manager.get_verification_config = AsyncMock(return_value=SimpleNamespace(
        welcome_message="*欢迎* {username} 加入群聊！"
    ))
    result = await manager.handle_new_member(user_id=702, chat_id=44, username=username)
    assert result.action == "welcome"
    assert result.message == f"*欢迎* {expected} 加入群聊！"
    assert result.session_id is None


@pytest.mark.asyncio
async def test_default_welcome_without_username_has_no_unescaped_underscore():
    manager = VerificationManager(db=None)
    manager.should_verify = AsyncMock(return_value=(False, "disabled"))
    manager.get_verification_config = AsyncMock(return_value=None)
    result = await manager.handle_new_member(user_id=702, chat_id=44, username=None)
    assert result.message == r"欢迎 User\_702 加入群聊！"
