from __future__ import annotations

from sqlalchemy import func, select

from app.modules.qq.models import QQBotConnection, QQGroupMessage, QQManagedGroup
from app.modules.qq.service import QQEventProcessor


async def test_onebot_group_message_registers_group_and_deduplicates(test_db) -> None:
    connection = QQBotConnection(app_id="10001", bot_openid="10001")
    test_db.add(connection)
    await test_db.flush()
    processor = QQEventProcessor(test_db)
    event = {
        "time": 1784100000,
        "self_id": 10001,
        "post_type": "message",
        "message_type": "group",
        "message_id": 9001,
        "group_id": 123456789,
        "user_id": 20002,
        "raw_message": "@10001 hello",
        "message": [
            {"type": "at", "data": {"qq": "10001"}},
            {"type": "text", "data": {"text": " hello"}},
            {"type": "image", "data": {"file": "a.png", "url": "https://img.test/a"}},
        ],
        "sender": {"user_id": 20002, "role": "member"},
    }

    created = await processor.handle_onebot_event(connection, event)
    duplicate = await processor.handle_onebot_event(connection, event)
    await test_db.commit()

    group = (
        await test_db.execute(
            select(QQManagedGroup).where(QQManagedGroup.group_openid == "123456789")
        )
    ).scalar_one()
    count = (
        await test_db.execute(
            select(func.count(QQGroupMessage.id)).where(QQGroupMessage.group_id == group.id)
        )
    ).scalar_one()

    assert created is not None
    assert created[1] == "qq:message"
    assert created[2]["content"] == "@10001 hello"
    assert created[2]["attachments"][0]["filename"] == "a.png"
    assert created[2]["is_at_account"] is True
    assert duplicate is None
    assert count == 1
    assert group.receive_all_messages_enabled is True
    assert group.proactive_messages_enabled is True


async def test_inactive_group_does_not_store_onebot_messages(test_db) -> None:
    connection = QQBotConnection(app_id="10001")
    test_db.add(connection)
    await test_db.flush()
    group = QQManagedGroup(
        connection_id=connection.id,
        group_openid="123456789",
        status="inactive",
    )
    test_db.add(group)
    await test_db.flush()
    processor = QQEventProcessor(test_db)

    result = await processor.handle_onebot_event(
        connection,
        {
            "time": 1784100000,
            "post_type": "message",
            "message_type": "group",
            "message_id": 9001,
            "group_id": 123456789,
            "user_id": 20002,
            "message": [{"type": "text", "data": {"text": "not stored"}}],
        },
    )
    count = (
        await test_db.execute(
            select(func.count(QQGroupMessage.id)).where(QQGroupMessage.group_id == group.id)
        )
    ).scalar_one()

    assert result is None
    assert count == 0
    assert group.status == "inactive"


async def test_onebot_group_sync_updates_names_without_reenabling_inactive_groups(test_db) -> None:
    connection = QQBotConnection(app_id="10001")
    test_db.add(connection)
    await test_db.flush()
    inactive = QQManagedGroup(
        connection_id=connection.id,
        group_openid="123456789",
        local_name="QQ 群 123456789",
        status="inactive",
    )
    test_db.add(inactive)
    await test_db.flush()

    groups = await QQEventProcessor(test_db).sync_groups(
        connection,
        [
            {"group_id": 123456789, "group_name": "Support"},
            {"group_id": 987654321, "group_name": "Alerts"},
        ],
    )

    assert len(groups) == 2
    assert inactive.local_name == "Support"
    assert inactive.status == "inactive"
    assert groups[1].local_name == "Alerts"
