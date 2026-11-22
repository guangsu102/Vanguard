import asyncio
from sqlalchemy import select, update
from app.core import database as db
from app.core.account.models import AccountSpamCheckItem, AccountSpamCheckOperation

async def main():
    await db.init_db()
    async with db.async_session_factory() as session:
        op = await session.get(AccountSpamCheckOperation, 42)
        print(f"Op 42 status: {op.status}")
        await session.execute(
            update(AccountSpamCheckItem)
            .where(AccountSpamCheckItem.operation_id == 42)
            .values(status="pending", next_retry_at=None)
        )
        await session.commit()
        print("Updated items to pending.")

if __name__ == "__main__":
    asyncio.run(main())
