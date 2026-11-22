import asyncio
from datetime import datetime
from sqlalchemy import select
from app.core.database import get_db, init_db
from app.modules.acquisition.simple_limits import durable_outbound_used
from app.core.account.models import TelegramAccount, AccountOperationConfig
from app.core.account.outbound_budget import effective_capacity_limits

async def run():
    await init_db()
    async for db in get_db():
        now = datetime.utcnow()
        for acc in [2, 33, 34, 35]:
            account = await db.get(TelegramAccount, acc)
            config = await db.scalar(select(AccountOperationConfig).where(AccountOperationConfig.account_id == acc))
            limits = await effective_capacity_limits(db, account, config, now)
            used = await durable_outbound_used(db, acc, now)
            print(f'Account {acc}: durable_outbound_used={used}, limits_total={limits["total"]}, limits_ad={limits["ad"]}')
        break

if __name__ == '__main__':
    asyncio.run(run())
