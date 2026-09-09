"""Atomic persistence helpers for acquisition conversation contexts."""

from datetime import datetime

from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.acquisition.models import ConversationContext


async def upsert_conversation_context(
    db: AsyncSession,
    *,
    user_id: int,
    context_data: str,
    expires_at: datetime,
    message_history: str | None = None,
    preserve_message_history: bool = False,
) -> None:
    """Atomically create or update the single context row for a Telegram user."""
    dialect_name = db.get_bind().dialect.name
    if dialect_name == "postgresql":
        insert_factory = postgresql_insert
    elif dialect_name == "sqlite":
        insert_factory = sqlite_insert
    else:
        raise RuntimeError(
            f"Unsupported conversation context upsert dialect: {dialect_name}"
        )

    now = datetime.utcnow()
    values = {
        "user_id": user_id,
        "context_data": context_data,
        "expires_at": expires_at,
        "updated_at": now,
    }
    if not preserve_message_history:
        values["message_history"] = message_history

    statement = insert_factory(ConversationContext).values(**values)
    update_values = {
        "context_data": statement.excluded.context_data,
        "expires_at": statement.excluded.expires_at,
        "updated_at": statement.excluded.updated_at,
    }
    if not preserve_message_history:
        update_values["message_history"] = statement.excluded.message_history

    await db.execute(
        statement.on_conflict_do_update(
            index_elements=[ConversationContext.user_id],
            set_=update_values,
        )
    )
    await db.commit()
