import asyncio
from app.core import database as db
from app.core.redis import init_redis
from app.core.account.models import TelegramAccount
from app.core.account.pool import get_account_pool

async def inspect():
    await db.init_db()
    await init_redis()
    pool = get_account_pool()
    async with db.async_session_factory() as s:
        a = await s.get(TelegramAccount, 35)
        await pool.sync_from_db([a])
    
    wrapper = await pool.acquire_by_id(35, purpose="perm_diag_detail")
    if not wrapper:
        print("Cannot acquire wrapper")
        return
    client = wrapper.client
    try:
        # Group 291: tg_id = -1001618231127
        entity = await client.get_entity(-1001618231127)
        perms = await client.get_permissions(entity, "me")
        print("=== PERMISSIONS OBJECT ===")
        for attr in dir(perms):
            if not attr.startswith("_"):
                print(f"  {attr}: {getattr(perms, attr)}")
        print("\n=== ENTITY ATTRIBUTES ===")
        print(f"  banned_rights: {getattr(entity, 'banned_rights', None)}")
        print(f"  default_banned_rights: {getattr(entity, 'default_banned_rights', None)}")
    finally:
        await pool.release(wrapper)

if __name__ == "__main__":
    asyncio.run(inspect())
