import asyncio
import uuid
from app.core import database as db
from app.core.redis import init_redis
from app.core.account.pool import get_account_pool
from app.modules.account_spam.service import create_spam_check_operation, run_account_spam_check_tick

async def main():
    await db.init_db()
    await init_redis()
    async with db.async_session_factory() as session:
        op, created = await create_spam_check_operation(
            session,
            account_ids=[33, 35],
            idempotency_key=uuid.uuid4().hex,
            created_by_id=1,
            trigger="manual"
        )
        print(f"Created Spam Check Operation {op.id}, created={created}")
        await session.commit()
        
    pool = get_account_pool()
    async with db.async_session_factory() as session:
        res = await run_account_spam_check_tick(session, limit=10, stale_after_seconds=300, account_pool=pool)
        print(f"Spam check tick result: {res}")

if __name__ == "__main__":
    asyncio.run(main())
