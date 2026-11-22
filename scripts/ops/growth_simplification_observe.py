"""Read-only production evidence; run on the canonical Compose host."""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CODE = r'''
import asyncio, json, os
from datetime import datetime, UTC
import asyncpg
from redis.asyncio import Redis

async def main():
    conn = await asyncpg.connect(os.environ['DATABASE_URL'].replace('postgresql+asyncpg:', 'postgresql:'), timeout=10)
    queries = {
      'connections': "select count(*) as connections, count(*) filter (where state='idle in transaction') as idle_transactions, max(extract(epoch from now()-xact_start)) as oldest_transaction from pg_stat_activity where usename='vanguard_app'",
      'connection_cap': "select rolconnlimit from pg_roles where rolname='vanguard_app'",
      'global_connections': "select usename,state,count(*) as count from pg_stat_activity group by usename,state",
      'global_limits': "select name,setting from pg_settings where name in ('max_connections','superuser_reserved_connections','reserved_connections')",
      'long_transactions': "select pid,client_addr,application_name,state,wait_event_type,wait_event,extract(epoch from now()-xact_start) as seconds,left(query,180) as statement from pg_stat_activity where usename='vanguard_app' and xact_start < now()-interval '5 seconds'",
      'transactions': "select xact_commit,xact_rollback,deadlocks from pg_stat_database where datname=current_database()",
      'joins': "select account_id,status,count(*) as count from acquisition_auto_join_attempt where attempted_at >= $1 group by account_id,status",
      'ads': "select account_id,status,count(*) as count,count(*) filter(where telegram_message_id>0) as positive_message_ids from ad_delivery_log where created_at >= $1 group by account_id,status",
      'duplicates': "select account_id,telegram_group_id,telegram_message_id,count(*) as count from ad_delivery_log where created_at >= $1 and telegram_message_id>0 group by account_id,telegram_group_id,telegram_message_id having count(*)>1",
      'nearby_sends': "with ordered as (select id,account_id,telegram_group_id,telegram_message_id,created_at,lag(created_at) over(partition by telegram_group_id order by created_at,id) as previous_at,lag(telegram_message_id) over(partition by telegram_group_id order by created_at,id) as previous_message_id from ad_delivery_log where created_at >= $1::timestamp - interval '1 day' and telegram_message_id>0) select * from ordered where created_at >= $1 and created_at-previous_at < interval '60 seconds' and telegram_message_id<>previous_message_id limit 20",
      'permissions': "select status,review_status,ad_status,count(*) as count from group_account_membership group by status,review_status,ad_status",
      'daily_review': "select count(*) filter(where daily_review_due_at<=timezone('UTC',now())) as due, min(daily_review_due_at) as next_due, max(daily_review_checked_at) as last_checked from group_ad_frequency",
      'daily_review_results': "select kind,old_quota,new_quota,count(*) as count,min(created_at) as first_at,max(created_at) as last_at from group_ad_frequency_event where created_at >= $1 and kind like 'daily_%' group by kind,old_quota,new_quota",
      'daily_review_completed': "select e.telegram_group_id,e.log_id,e.kind,e.old_quota,e.new_quota,e.created_at,l.telegram_message_id,l.sent_at,l.survival_check_due_at,l.survival_status from group_ad_frequency_event e join ad_delivery_log l on l.id=e.log_id where e.created_at >= $1 and e.kind like 'daily_%' order by e.created_at desc limit 50",
      'daily_review_pending': "select telegram_group_id,daily_review_log_id,daily_review_due_at,daily_review_checked_at,daily_review_retry_at,daily_review_error from group_ad_frequency where daily_review_due_at<=timezone('UTC',now()) order by daily_review_due_at limit 20",
      'accounts': "select id,status,risk_level from telegram_account where lower(account_type::text)='promoter'",
      'account_controls': "select c.account_id,c.enabled,c.auto_join_enabled,c.auto_ads_enabled,c.operation_mode,c.business_stage,c.next_join_after from telegram_account_operation_config c join telegram_account a on a.id=c.account_id where lower(a.account_type::text)='promoter'",
      'retired_growth_writes': "select 'qualification_work' as relation,count(*) as changed from qualification_work where updated_at >= $1 union all select 'qualification_active_grant',count(*) from qualification_active_grant where updated_at >= $1 union all select 'ad_delivery_schedule_state',count(*) from ad_delivery_schedule_state where updated_at >= $1",
      'new_outbound_attempts': "select category,state,count(*) as count from account_outbound_attempt where created_at >= $1 group by category,state",
      'permission_identities': "select account_id,count(*) filter(where telegram_group_id>0) as legacy_positive,count(*) filter(where telegram_group_id<0) as marked from group_account_membership where status='joined' and review_status='approved' group by account_id",
    }
    started = datetime.fromisoformat(os.environ['OBSERVATION_SINCE']).replace(tzinfo=None)
    result = {'at':datetime.now(UTC).isoformat()}
    try:
        for key,query in queries.items():
            try:
                rows = await conn.fetch(query, *([started] if '$1' in query else []), timeout=10)
                result[key] = [dict(r) for r in rows]
            except Exception as exc:
                result[key] = {'error':type(exc).__name__, 'message':str(exc)[:200]}
    finally:
        await conn.close()
    redis = Redis.from_url(os.environ['REDIS_URL'],decode_responses=True)
    try:
        for version in ('v1','v2'):
            prefix = 'vanguard:growth_dispatch:'+version+':'
            result[version] = {'dispatch':json.loads(await redis.get(prefix+'last_dispatch') or '{}'),
                'runs':{k:json.loads(v) for k,v in (await redis.hgetall(prefix+'last_runs')).items()
                        if json.loads(v).get('at',0) >= started.replace(tzinfo=UTC).timestamp()}}
    finally:
        await redis.aclose()
    print('OBSERVATION_JSON='+json.dumps(result,default=str))
asyncio.run(main())
'''


def sample():
    marker = next(ROOT / name for name in ('p1-started.json', 'p0-resumed.json', 'p0-started.json')
                  if (ROOT / name).exists())
    started = json.loads(marker.read_text())['started_at']
    proc = subprocess.run(['docker', 'exec', '-i', '-e', f'OBSERVATION_SINCE={started}',
                           'vanguard-backend', 'python', '-'], input=CODE, text=True,
                          capture_output=True, check=True, timeout=120)
    line = next(s for s in proc.stdout.splitlines() if s.startswith('OBSERVATION_JSON='))
    result = json.loads(line.split('=', 1)[1])
    path = ROOT / 'observations'
    path.mkdir(exist_ok=True)
    name = datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ') + '.json'
    (path / name).write_text(json.dumps(result, indent=2))
    print(json.dumps({k: result[k] for k in ('at','connections','joins','ads','accounts','long_transactions')}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=int, default=0)
    parser.add_argument('--interval', type=int, default=30)
    args = parser.parse_args()
    if args.interval < 1 or args.seconds < 0:
        parser.error('interval must be positive and seconds must be nonnegative')
    deadline = time.monotonic() + args.seconds
    while True:
        try:
            sample()
        except Exception as exc:
            print(json.dumps({'at': datetime.now(UTC).isoformat(),
                              'observation_error': type(exc).__name__}), flush=True)
            if args.seconds == 0:
                raise
        if time.monotonic() >= deadline:
            return
        time.sleep(min(args.interval, max(0, deadline - time.monotonic())))


if __name__ == '__main__':
    main()
