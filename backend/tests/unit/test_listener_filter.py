import time
from types import SimpleNamespace as Obj

from app.core.account.listener_filter import keep_message


def event(**changes):
    values = {
        "chat_id": -10022,
        "is_private": False,
        "raw_text": "ordinary chat",
        "sender": Obj(bot=False),
        "message": Obj(),
    }
    values.update(changes)
    return Obj(**values)


def test_disabled_consumers_skip_chatter_preserve_direct_and_owned_events():
    policy = {"loaded_at": time.monotonic(), "ordinary_required": False, "owned_chats": {-10033}}
    assert not keep_message(event(), policy)
    for item in [
        event(is_private=True),
        event(chat_id=-10033),
        event(raw_text="/help"),
        event(sender=Obj(bot=True)),
        event(message=Obj(mentioned=True)),
        event(message=Obj(reply_to_msg_id=9)),
    ]:
        assert keep_message(item, policy)
    assert keep_message(event(), {**policy, "ordinary_required": True})
    assert keep_message(event(), {**policy, "loaded_at": time.monotonic() - 91})
    assert keep_message(event(), None)
