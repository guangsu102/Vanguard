import asyncio
from sqlalchemy import text
from app.core.database import init_db, get_db_session, close_db

async def main():
    await init_db(create_tables=False)
    async with get_db_session() as db:
        res = await db.execute(text("""
            SELECT account_id, review_status, ad_status, COUNT(*) as count
            FROM group_account_membership
            WHERE status = 'joined' AND left_at IS NULL
            GROUP BY account_id, review_status, ad_status
            ORDER BY account_id, count DESC
        """))
        for r in res.fetchall():
            print(dict(r._mapping))
    await close_db()

if __name__ == '__main__':
    asyncio.run(main())
