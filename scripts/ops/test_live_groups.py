import asyncio
from sqlalchemy import select
from app.core import database as db
from app.core.group.models import Group, GroupAccountMembership
from app.core.account.pool import get_account_pool

async def test_acc_groups(account_id: int):
    pool = get_account_pool()
    async with db.async_session_factory() as session:
        # Get all approved active groups for this account
        mems = (await session.execute(
            select(GroupAccountMembership, Group)
            .join(Group, GroupAccountMembership.group_id == Group.id)
            .where(
                GroupAccountMembership.account_id == account_id,
                GroupAccountMembership.status == "joined",
                GroupAccountMembership.review_status == "approved",
                GroupAccountMembership.ad_status == "active"
            )
            .limit(10)
        )).all()
        print(f"=== TESTING FIRST {len(mems)} APPROVED GROUPS FOR ACC {account_id} ===")
        
        wrapper = await pool.acquire_by_id(account_id, purpose="perm_diag")
        if not wrapper or not wrapper.client:
            print(f"Could not acquire client for acc {account_id}")
            return
        client = wrapper.client
        try:
            for mem, grp in mems:
                tg_id = mem.telegram_group_id or grp.group_id
                try:
                    entity = await client.get_entity(tg_id)
                    perms = await client.get_permissions(entity, "me")
                    is_banned = getattr(perms, "is_banned", False)
                    has_left = getattr(perms, "has_left", False)
                    is_admin = getattr(perms, "is_admin", False)
                    send_messages = getattr(perms, "send_messages", None)
                    print(f"Group {grp.id} ({grp.title}) [{tg_id}]: is_banned={is_banned}, has_left={has_left}, is_admin={is_admin}, send_msg={send_messages}")
                except Exception as e:
                    print(f"Group {grp.id} ({grp.title}) [{tg_id}]: EXCEPTION {type(e).__name__}: {e}")
        finally:
            await pool.release(wrapper)

async def main():
    await db.init_db()
    print("Testing Account 33...")
    await test_acc_groups(33)
    print("\nTesting Account 35...")
    await test_acc_groups(35)

if __name__ == "__main__":
    asyncio.run(main())
