import asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, func, desc
from app.core.database import init_db, get_db
from app.core.account.models import TelegramAccount
from app.core.group.models import Group, GroupAccountMembership
from app.modules.acquisition.models import AdDeliveryLog, GroupAdFrequency, GroupSearchKeyword, AutoJoinAttempt
from app.core.redis import init_redis, get_redis

async def inspect():
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    
    print(f"=== PRODUCTION AUDIT AT {now.strftime('%Y-%m-%d %H:%M:%S UTC')} ===")
    await init_db()
    await init_redis()
    async for db in get_db():
        # 1. Accounts
        stmt = select(TelegramAccount).where(TelegramAccount.id.in_([2, 33, 34, 35]))
        res = await db.execute(stmt)
        accounts = res.scalars().all()
        print("\n--- 1. PROMOTER ACCOUNTS ---")
        for acc in accounts:
            print(f"Account {acc.id} ({acc.phone}): status={acc.status}, is_active={acc.is_active}, "
                  f"risk_score={acc.risk_score}, spam={acc.spam_check_status}")
            
        # 2. Ad Delivery Stats Today (UTC)
        stmt = (
            select(AdDeliveryLog.account_id, AdDeliveryLog.status, func.count(AdDeliveryLog.id))
            .where(AdDeliveryLog.created_at >= today_start)
            .group_by(AdDeliveryLog.account_id, AdDeliveryLog.status)
        )
        res = await db.execute(stmt)
        stats = res.all()
        print(f"\n--- 2. AD DELIVERIES TODAY (Since {today_start.strftime('%Y-%m-%d %H:%M:%S')}) ---")
        by_acc = {}
        for acc_id, status, count in stats:
            by_acc.setdefault(acc_id, {})[status] = count
            
        for acc_id in sorted([2, 33, 34, 35]):
            acc_stats = by_acc.get(acc_id, {})
            succ = acc_stats.get('success', 0)
            perm = acc_stats.get('permission_changed', 0)
            rate_lim = acc_stats.get('rate_limited', 0)
            other = sum(v for k, v in acc_stats.items() if k not in ('success', 'permission_changed', 'rate_limited'))
            print(f"Account {acc_id}: success={succ}, banned/permission_changed={perm}, rate_limited={rate_lim}, other={other}, total_attempts={sum(acc_stats.values())}")
            
        # 3. Latest 15 deliveries
        stmt = select(AdDeliveryLog).order_by(desc(AdDeliveryLog.id)).limit(15)
        res = await db.execute(stmt)
        recent_ads = res.scalars().all()
        print("\n--- 3. LATEST 15 AD DELIVERY LOGS ---")
        for ad in recent_ads:
            err_msg = (ad.error or "")[:60]
            print(f"Log {ad.id}: acc={ad.account_id}, grp={ad.group_id}, status={ad.status}, time={ad.created_at}, err={err_msg}")
            
        # 4. Group inventory by review & ad status
        stmt = select(GroupAccountMembership.account_id, GroupAccountMembership.status, func.count(GroupAccountMembership.id)).group_by(GroupAccountMembership.account_id, GroupAccountMembership.status)
        res = await db.execute(stmt)
        memberships = res.all()
        print("\n--- 4. GROUP MEMBERSHIP STATUS PER ACCOUNT ---")
        mem_by_acc = {}
        for acc_id, m_status, count in memberships:
            mem_by_acc.setdefault(acc_id, {})[m_status] = count
        for acc_id in sorted([2, 33, 34, 35]):
            m = mem_by_acc.get(acc_id, {})
            print(f"Account {acc_id}: {dict(m)}")
            
        # GroupAccountMembership breakdown by review_status and ad_status
        stmt = select(GroupAccountMembership.account_id, GroupAccountMembership.review_status, GroupAccountMembership.ad_status, func.count(GroupAccountMembership.id)).group_by(GroupAccountMembership.account_id, GroupAccountMembership.review_status, GroupAccountMembership.ad_status)
        res = await db.execute(stmt)
        mem_details = res.all()
        print("\n--- 5. MEMBERSHIP (review_status / ad_status) PER ACCOUNT ---")
        for acc_id in sorted([2, 33, 34, 35]):
            details = [f"{r}/{a}:{c}" for aid, r, a, c in mem_details if aid == acc_id]
            print(f"Account {acc_id}: {', '.join(details)}")
            
        # Overall approved+active ready for ads
        stmt = select(func.count(GroupAccountMembership.id)).where(GroupAccountMembership.review_status == 'approved', GroupAccountMembership.ad_status == 'active', GroupAccountMembership.status == 'joined')
        ready_joined_cnt = (await db.execute(stmt)).scalar() or 0
        stmt = select(func.count(GroupAccountMembership.id)).where(GroupAccountMembership.review_status == 'pending', GroupAccountMembership.status == 'joined')
        pending_review_cnt = (await db.execute(stmt)).scalar() or 0
        print(f"\nOverall joined: Ready for Ads (approved+active)={ready_joined_cnt}, Pending review={pending_review_cnt}")

        # Keywords & Joins today
        stmt = select(func.count(GroupSearchKeyword.id)).where(GroupSearchKeyword.status == 'approved')
        approved_keywords = (await db.execute(stmt)).scalar() or 0
        stmt = select(func.count(GroupSearchKeyword.id)).where(GroupSearchKeyword.status == 'pending')
        pending_keywords = (await db.execute(stmt)).scalar() or 0
        stmt = select(AutoJoinAttempt.status, func.count(AutoJoinAttempt.id)).where(AutoJoinAttempt.attempted_at >= today_start).group_by(AutoJoinAttempt.status)
        joins_today = (await db.execute(stmt)).all()
        print(f"\n--- 6. KEYWORDS & JOINS TODAY ---")
        print(f"Approved keywords: {approved_keywords}, Pending keywords: {pending_keywords}")
        print(f"Joins attempted today: {dict(joins_today)}")
        break

    # 7. Redis State (due times & rate limits)
    r = await get_redis()
    print("\n--- 6. REDIS SCHEDULING KEYS ---")
    for acc_id in [2, 33, 34, 35]:
        ad_count = await r.get(f"vanguard:growth:v2:account:{acc_id}:ad_count")
        acc_due = await r.get(f"vanguard:growth:v2:account:{acc_id}:ads:due")
        print(f"Account {acc_id} in Redis: ad_count today = {ad_count}, ads:due = {acc_due}")

    ktype = await r.type("vanguard:growth_dispatch:v2:ads:due")
    if ktype == "string":
        dispatch_due = await r.get("vanguard:growth_dispatch:v2:ads:due")
    elif ktype == "zset":
        dispatch_due = await r.zrange("vanguard:growth_dispatch:v2:ads:due", 0, -1, withscores=True)
    else:
        dispatch_due = f"(type: {ktype})"
    print(f"Growth dispatch ads:due = {dispatch_due}")
    
    # Check if any leases are held
    leases = await r.keys("vanguard:telegram:lease:*")
    print(f"Active Telegram leases: {[k.decode() if isinstance(k, bytes) else k for k in leases]}")

asyncio.run(inspect())
