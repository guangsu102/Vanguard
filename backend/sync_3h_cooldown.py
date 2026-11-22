import asyncio
import time
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, func
from app.core.database import get_db, init_db
from app.core.redis import init_redis, get_redis
from app.core.group.models import GroupAccountMembership as Member, Group
from app.modules.acquisition.models import AdDeliveryLog

async def run():
    await init_db()
    await init_redis()
    redis = await get_redis()
    now_ts = time.time()
    now_dt = datetime.now(timezone.utc).replace(tzinfo=None)
    
    # 1. Inspect and repair Redis group due keys
    keys = await redis.keys('vanguard:growth:v2:group:*:due')
    print(f'Total group due keys in Redis: {len(keys)}')
    repaired_redis = 0
    async for db in get_db():
        for k in keys:
            raw_tg_id = k.split(':')[-2]
            try:
                tg_id = int(raw_tg_id)
            except ValueError:
                continue
            
            val = await redis.get(k)
            if not val:
                continue
            due_ts = float(val)
            rem = due_ts - now_ts
            if rem > 10800: # greater than 3 hours
                grp = await db.scalar(select(Group).where(Group.group_id == tg_id))
                last_sent = None
                if grp:
                    last_sent = await db.scalar(
                        select(func.max(AdDeliveryLog.created_at))
                        .where(AdDeliveryLog.group_id == grp.id, AdDeliveryLog.status == 'success')
                    )
                
                if last_sent:
                    new_due_dt = last_sent + timedelta(hours=3)
                    new_due_ts = new_due_dt.replace(tzinfo=timezone.utc).timestamp()
                else:
                    new_due_dt = now_dt
                    new_due_ts = now_ts
                
                if new_due_ts <= now_ts:
                    await redis.delete(k)
                    print(f'Group {tg_id}: deleted expired due key (last_sent was {last_sent})')
                else:
                    new_ttl = int(new_due_ts - now_ts)
                    await redis.set(k, str(int(new_due_ts)), ex=new_ttl)
                    print(f'Group {tg_id}: updated due to {new_ttl}s remaining (last_sent was {last_sent})')
                repaired_redis += 1
                
        # 2. Check and repair Member.next_ad_allowed_at in PostgreSQL
        stale_mems = (await db.scalars(select(Member).where(
            Member.next_ad_allowed_at > now_dt + timedelta(hours=3)
        ))).all()
        print(f'Members with next_ad_allowed_at > 3 hours: {len(stale_mems)}')
        for m in stale_mems:
            last_sent = await db.scalar(
                select(func.max(AdDeliveryLog.created_at))
                .where(AdDeliveryLog.group_id == m.group_id, AdDeliveryLog.status == 'success')
            )
            if last_sent:
                m.next_ad_allowed_at = last_sent + timedelta(hours=3)
            else:
                m.next_ad_allowed_at = now_dt
            print(f'Member {m.id} (grp {m.group_id}): adjusted next_ad_allowed_at to {m.next_ad_allowed_at}')
        await db.commit()
        
        # 3. Reset dispatch due for ads in Redis
        await redis.delete('vanguard:growth_dispatch:v2:ads:due')
        print('Reset vanguard:growth_dispatch:v2:ads:due')
        break

if __name__ == '__main__':
    asyncio.run(run())
