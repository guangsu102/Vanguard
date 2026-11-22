import asyncio
from sqlalchemy import select, desc
from app.core.database import get_db, init_db
from app.modules.acquisition.models import AdDeliveryLog

async def run():
    await init_db()
    async for db in get_db():
        logs = (await db.scalars(select(AdDeliveryLog).order_by(desc(AdDeliveryLog.id)).limit(10))).all()
        print('=== LATEST 10 AD DELIVERY LOGS ===')
        for l in logs:
            print(f'Log {l.id}: acc={l.account_id}, grp={l.group_id}, creative={l.creative_id}, status={l.status}, time={l.created_at}, err={l.error}')
        break

if __name__ == '__main__':
    asyncio.run(run())
