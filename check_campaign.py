import asyncio
from sqlalchemy import text
from app.core.database import init_db, get_db_session, close_db

async def main():
    await init_db(create_tables=False)
    async with get_db_session() as db:
        res = await db.execute(text("""
            SELECT id, name, enabled, send_mode, min_wait_after_join_minutes,
                   interval_minutes, max_sends_per_group_per_day, max_sends_per_account_per_day
            FROM ad_campaign
        """))
        for r in res.fetchall():
            print(dict(r._mapping))
    await close_db()

if __name__ == '__main__':
    asyncio.run(main())
