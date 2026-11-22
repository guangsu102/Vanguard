import asyncio
from sqlalchemy import select, update
from app.core import database as db
from app.core.account.models import AccountSpamCheckItem

async def main():
    await db.init_db()
    async with db.async_session_factory() as session:
        # Reset retry_wait to queued so it runs immediately
        await session.execute(
            update(AccountSpamCheckItem)
            .where(AccountSpamCheckItem.operation_id == 42)
            .values(status="queued", next_retry_at=None)
        )
        await session.commit()
        print("Reset operation 42 items to queued.")

if __name__ == "__main__":
    asyncio.run(main())
