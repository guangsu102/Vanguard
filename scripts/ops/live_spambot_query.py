import asyncio
from app.core import database as db
from app.core.redis import init_redis
from app.core.account.models import TelegramAccount
from app.core.account.pool import get_account_pool

async def ask_bot(account_id: int):
    pool = get_account_pool()
    async with db.async_session_factory() as session:
        acc = await session.get(TelegramAccount, account_id)
        if not acc:
            print(f"Account {account_id} not found")
            return
        await pool.sync_from_db([acc])
        
    wrapper = await pool.acquire_by_id(account_id, purpose="spambot_direct_query")
    if not wrapper or not wrapper.client:
        print(f"Could not acquire client for {account_id}")
        return
    client = wrapper.client
    try:
        bot = await client.get_entity("SpamBot")
        await client.send_message(bot, "/start")
        await asyncio.sleep(3)
        msgs = await client.get_messages(bot, limit=2)
        print(f"=== ACCOUNT {account_id} ({acc.phone}) SPAMBOT REPLY ===")
        for m in msgs:
            if not m.out:
                print(f"Reply: {m.text}")
    except Exception as e:
        print(f"Account {account_id} error querying SpamBot: {type(e).__name__}: {e}")
    finally:
        await pool.release(wrapper)

async def main():
    await db.init_db()
    await init_redis()
    print("Checking Account 33...")
    await ask_bot(33)
    print("\nChecking Account 35...")
    await ask_bot(35)

if __name__ == "__main__":
    asyncio.run(main())
