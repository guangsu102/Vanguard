"""Check account 33 and 35 state and auto-join candidates."""
import asyncio
from sqlalchemy import text
from app.core import database as db


async def check():
    async with db.session() as session:
        # Check account status
        r = await session.execute(text("""
            SELECT id, phone, status, spam_check_status, is_active
            FROM telegram_accounts
            WHERE id IN (33, 35)
        """))
        print("=== ACCOUNT STATUS ===")
        for row in r.fetchall():
            print(dict(row._mapping))

        # Check memberships for 33/35
        r2 = await session.execute(text("""
            SELECT
                gm.account_id,
                gm.review_status,
                gm.can_send_text,
                gm.status,
                COUNT(*) as cnt
            FROM group_memberships gm
            WHERE gm.account_id IN (33, 35)
            GROUP BY gm.account_id, gm.review_status, gm.can_send_text, gm.status
            ORDER BY gm.account_id, cnt DESC
        """))
        print("\n=== MEMBERSHIP STATUS BREAKDOWN ===")
        for row in r2.fetchall():
            print(dict(row._mapping))

        # Check candidate groups (not yet joined)
        r3 = await session.execute(text("""
            SELECT a.id as acc_id, COUNT(g.id) as candidate_groups
            FROM telegram_accounts a
            CROSS JOIN groups g
            LEFT JOIN group_memberships gm ON gm.group_id = g.id AND gm.account_id = a.id
            WHERE a.id IN (33, 35)
              AND g.status IN ('pending', 'active')
              AND gm.id IS NULL
            GROUP BY a.id
        """))
        print("\n=== UNJOINED CANDIDATE GROUPS FOR 33/35 ===")
        for row in r3.fetchall():
            print(dict(row._mapping))

        # Check groups with active approved memberships (can send ads)
        r4 = await session.execute(text("""
            SELECT
                gm.account_id,
                COUNT(*) as sendable_memberships
            FROM group_memberships gm
            JOIN groups g ON g.id = gm.group_id
            WHERE gm.account_id IN (33, 35)
              AND gm.review_status = 'approved'
              AND gm.can_send_text = true
              AND gm.status = 'joined'
              AND g.status = 'active'
            GROUP BY gm.account_id
        """))
        print("\n=== SENDABLE MEMBERSHIPS (approved+can_send_text+joined+active_group) ===")
        rows = r4.fetchall()
        if rows:
            for row in rows:
                print(dict(row._mapping))
        else:
            print("NONE - No sendable memberships for accounts 33 or 35")

        # What do memberships in active groups look like?
        r5 = await session.execute(text("""
            SELECT
                gm.account_id,
                g.status as group_status,
                gm.status as mem_status,
                gm.review_status,
                gm.can_send_text,
                COUNT(*) as cnt
            FROM group_memberships gm
            JOIN groups g ON g.id = gm.group_id
            WHERE gm.account_id IN (33, 35)
              AND g.status = 'active'
            GROUP BY gm.account_id, g.status, gm.status, gm.review_status, gm.can_send_text
            ORDER BY gm.account_id
        """))
        print("\n=== MEMBERSHIPS IN ACTIVE GROUPS ===")
        rows = r5.fetchall()
        if rows:
            for row in rows:
                print(dict(row._mapping))
        else:
            print("NONE - No memberships in active groups for 33/35")


asyncio.run(check())
