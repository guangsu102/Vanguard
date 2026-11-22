import asyncio
from app.core import database as db
from app.core.redis import init_redis
from app.core.account.models import TelegramAccount
from app.core.account.pool import get_account_pool
from telethon.tl.types import Channel, Chat

async def check_dialogs(account_id: int):
    pool = get_account_pool()
    async with db.async_session_factory() as session:
        account = await session.get(TelegramAccount, account_id)
        if not account:
            print(f"Account {account_id} not found in DB")
            return
        await pool.sync_from_db([account])
        
    wrapper = await pool.acquire_by_id(account_id, purpose="dialog_diag")
    if not wrapper or not wrapper.client:
        print(f"Failed to acquire client for {account_id}")
        return
    client = wrapper.client
    try:
        dialogs = await client.get_dialogs()
        groups = []
        for d in dialogs:
            if d.is_group or d.is_channel:
                entity = d.entity
                if isinstance(entity, Channel) and not entity.broadcast:
                    groups.append((d.id, d.title, getattr(entity, "username", None)))
                elif isinstance(entity, Chat):
                    groups.append((d.id, d.title, None))
        print(f"=== ACCOUNT {account_id} TELEGRAM DIALOGS: {len(groups)} GROUPS FOUND ===")
        for gid, title, uname in groups[:15]:
            print(f"  ID: {gid}, title: {title}, username: @{uname}")
        if len(groups) > 15:
            print(f"  ... and {len(groups) - 15} more groups")
    finally:
        await pool.release(wrapper)

async def main():
    await db.init_db()
    await init_redis()
    await check_dialogs(33)
    print()
    await check_dialogs(35)

if __name__ == "__main__":
    asyncio.run(main())
