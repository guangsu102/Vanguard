import asyncio
from app.core import database as db
from app.core.redis import init_redis
from app.core.account.models import TelegramAccount
from app.core.account.pool import get_account_pool

async def check_spambot_detail(account_id: int):
    pool = get_account_pool()
    async with db.async_session_factory() as session:
        acc = await session.get(TelegramAccount, account_id)
        await pool.sync_from_db([acc])
        
    wrapper = await pool.acquire_by_id(account_id, purpose="spambot_full_diag")
    if not wrapper or not wrapper.client:
        print(f"Could not acquire client for {account_id}")
        return
    client = wrapper.client
    try:
        bot = await client.get_entity("SpamBot")
        msgs = await client.get_messages(bot, limit=5)
        print(f"=== FULL SPAMBOT DIALOG FOR ACCOUNT {account_id} ({acc.phone}) ===")
        for m in reversed(msgs):
            sender = "ME" if m.out else "BOT"
            print(f"[{sender}] {m.date}: {m.text}")
            if getattr(m, 'reply_markup', None):
                print(f"  [MARKUP]: {m.reply_markup}")
    finally:
        await pool.release(wrapper)

async def main():
    await db.init_db()
    await init_redis()
    await check_spambot_detail(33)
    print()
    await check_spambot_detail(35)

if __name__ == "__main__":
    asyncio.run(main())
