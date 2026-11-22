import asyncio
from sqlalchemy import select
from app.core import database as db
from app.core.redis import init_redis
from app.core.account.models import TelegramAccount
from app.core.group.models import Group, GroupAccountMembership
from app.core.account.pool import get_account_pool

async def test_acc_groups(account_id: int):
    pool = get_account_pool()
    async with db.async_session_factory() as session:
        account = await session.get(TelegramAccount, account_id)
        await pool.sync_from_db([account])
        wrapper = await pool.acquire_by_id(account_id, purpose="perm_diag")
        if not wrapper or not wrapper.client:
            print(f"Could not acquire client for acc {account_id}")
            return
        client = wrapper.client
        try:
            mems = (await session.execute(
                select(GroupAccountMembership, Group)
                .join(Group, GroupAccountMembership.group_id == Group.id)
                .where(
                    GroupAccountMembership.account_id == account_id,
                    GroupAccountMembership.status == "joined",
                    GroupAccountMembership.review_status == "approved",
                    GroupAccountMembership.ad_status == "active"
                )
                .limit(5)
            )).all()
            print(f"=== TESTING FIRST {len(mems)} APPROVED GROUPS FOR ACC {account_id} ===")
            for mem, grp in mems:
                tg_id = mem.telegram_group_id or grp.group_id
                try:
                    entity = await client.get_entity(tg_id)
                    perms = await client.get_permissions(entity, "me")
                    is_banned = getattr(perms, "is_banned", False)
                    print(f"Grp {grp.id} ({grp.title}): is_banned={is_banned}")
                except Exception as e:
                    print(f"Grp {grp.id} ({grp.title}): EXCEPTION {type(e).__name__}: {e}")
        finally:
            await pool.release(wrapper)

async def main():
    await db.init_db()
    await init_redis()
    await test_acc_groups(35)

if __name__ == "__main__":
    asyncio.run(main())
