import asyncio
from sqlalchemy import select
from app.core import database as db
from app.core.account.models import AccountSpamCheckItem

async def main():
    await db.init_db()
    async with db.async_session_factory() as session:
        items = (await session.execute(
            select(AccountSpamCheckItem).where(AccountSpamCheckItem.operation_id == 42)
        )).scalars().all()
        for it in items:
            print(f"Item {it.id}: acc={it.account_id}, status={it.status}, result={it.result}, summary={it.response_summary}")

if __name__ == "__main__":
    asyncio.run(main())
