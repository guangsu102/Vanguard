from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.scheduler import tasks
from app.modules.acquisition.context_store import upsert_conversation_context
from app.modules.acquisition.models import ConversationContext


@pytest.mark.asyncio
async def test_context_upsert_keeps_one_row_per_user(test_db):
    now = datetime.utcnow()

    await upsert_conversation_context(
        test_db,
        user_id=123456,
        context_data='{"step":1}',
        message_history="[]",
        expires_at=now + timedelta(minutes=60),
    )
    await upsert_conversation_context(
        test_db,
        user_id=123456,
        context_data='{"step":2}',
        message_history='[{"direction":"inbound"}]',
        expires_at=now + timedelta(minutes=120),
    )

    count = (
        await test_db.execute(
            select(func.count(ConversationContext.id)).where(
                ConversationContext.user_id == 123456
            )
        )
    ).scalar_one()
    context = (
        await test_db.execute(
            select(ConversationContext).where(
                ConversationContext.user_id == 123456
            )
        )
    ).scalar_one()
    assert count == 1
    assert context.context_data == '{"step":2}'
    assert context.message_history == '[{"direction":"inbound"}]'


@pytest.mark.asyncio
async def test_context_upsert_can_preserve_dialog_history(test_db):
    now = datetime.utcnow()
    await upsert_conversation_context(
        test_db,
        user_id=654321,
        context_data='{"state":"init"}',
        message_history='[{"message":"hello"}]',
        expires_at=now + timedelta(minutes=60),
    )
    await upsert_conversation_context(
        test_db,
        user_id=654321,
        context_data='{"metadata":"updated"}',
        expires_at=now + timedelta(minutes=120),
        preserve_message_history=True,
    )

    context = (
        await test_db.execute(
            select(ConversationContext).where(
                ConversationContext.user_id == 654321
            )
        )
    ).scalar_one()
    assert context.context_data == '{"metadata":"updated"}'
    assert context.message_history == '[{"message":"hello"}]'


@pytest.mark.asyncio
async def test_cleanup_old_messages_deletes_only_expired_contexts(
    monkeypatch,
    test_db,
):
    now = datetime.utcnow()
    test_db.add_all(
        [
            ConversationContext(
                user_id=700001,
                context_data="{}",
                expires_at=now - timedelta(minutes=1),
            ),
            ConversationContext(
                user_id=700002,
                context_data="{}",
                expires_at=now + timedelta(hours=1),
            ),
        ]
    )
    await test_db.commit()

    async def run_with_test_db(handler):
        return await handler(test_db)

    monkeypatch.setattr(tasks, "_run_with_db", run_with_test_db)
    result = await tasks._cleanup_old_messages_async(
        batch_size=100,
        max_batches=2,
    )

    remaining = set(
        (
            await test_db.execute(select(ConversationContext.user_id))
        ).scalars().all()
    )
    assert result["deleted"] == 1
    assert remaining == {700002}


def test_context_expiry_index_is_declared():
    index_names = {
        index.name for index in ConversationContext.__table__.indexes
    }
    assert "idx_acquisition_conversation_context_expires" in index_names
