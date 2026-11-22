import asyncio
import os
import sys

sys.path.append("/app")

from app.core import database as db
from app.modules.acquisition.models import AdDeliveryLog
from sqlalchemy import select

async def main():
    await db.init_db()
    async with db.async_session_factory() as session:
        logs = (await session.scalars(select(AdDeliveryLog).order_by(AdDeliveryLog.id.desc()).limit(10))).all()
        print("\nLatest 10 AdDeliveryLogs:")
        for l in logs:
            print(f"  id={l.id}, acc={l.account_id}, grp={l.group_id}, status={l.status}, msg_id={l.telegram_message_id}, err={l.error}, created_at={l.created_at}")

if __name__ == "__main__":
    asyncio.run(main())
