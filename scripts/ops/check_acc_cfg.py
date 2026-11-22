import asyncio
import os
import sys

sys.path.append("/app")

from app.core import database as db
from app.core.account.models import AccountOperationConfig, TelegramAccount
from sqlalchemy import select

async def main():
    await db.init_db()
    async with db.async_session_factory() as session:
        for acc_id in [2, 33, 34, 35]:
            acc = await session.get(TelegramAccount, acc_id)
            cfg = await session.scalar(select(AccountOperationConfig).where(AccountOperationConfig.account_id == acc_id))
            print(f"Account {acc_id}:")
            print(f"  status={acc.status}, is_active={acc.is_active}, risk_level={acc.risk_level}, type={acc.account_type}")
            if cfg:
                print(f"  cfg: enabled={cfg.enabled}, mode={cfg.operation_mode}, auto_ads={cfg.auto_ads_enabled}, auto_join={cfg.auto_join_enabled}")
            else:
                print("  cfg: NONE")

if __name__ == "__main__":
    asyncio.run(main())
