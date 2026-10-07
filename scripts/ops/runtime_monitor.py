"""Host-local monitor; persists actionable alerts to the authenticated dashboard.

No Telegram/email messages are sent. systemd journals alert transitions.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

ROOT = Path('/opt/vanguard')
STATUS = ROOT / 'ops/runtime-status.json'


def inspect():
    alerts = []
    disk = shutil.disk_usage(ROOT)
    used = (disk.total - disk.free) / disk.total
    if used > .8:
        alerts.append({'code': 'host_disk_high', 'severity': 'critical' if used > .9 else 'warning'})
    latest = ROOT / 'scheduled-backups/latest.json'
    backup = json.loads(latest.read_text()) if latest.exists() else {}
    age = time.time() - backup.get('completed_at', 0)
    if age > 30 * 3600:
        alerts.append({'code': 'backup_stale', 'severity': 'critical'})
    names = ['vanguard-' + name for name in ('backend', 'frontend', 'redis', 'celery-worker',
             'celery-beat', 'resource-search-worker', 'telegram-growth-worker', 'telegram-guardian-worker')]
    names += ['sub2api-dr-postgres', 'oracle-shared-postgres-gateway']
    rows = json.loads(subprocess.check_output(['docker', 'inspect', *names], text=True, timeout=20))
    for row in rows:
        state = row['State']
        if not state['Running'] or state.get('OOMKilled') or state.get('Health', {}).get('Status') == 'unhealthy':
            alerts.append({'code': 'container_unhealthy', 'service': row['Name'].lstrip('/'), 'severity': 'critical'})
    return {'checked_at': time.time(), 'disk_used_ratio': round(used, 4), 'disk_free_bytes': disk.free,
            'backup_age_seconds': round(age), 'alerts': alerts}


def main():
    os.umask(0o077)
    data = inspect()
    STATUS.parent.mkdir(mode=0o700, exist_ok=True)
    previous = json.loads(STATUS.read_text()) if STATUS.exists() else {}
    code = '''import asyncio,json,sys
from app.core.database import init_db,get_db_session,close_db
from app.core.redis import init_redis,get_redis,close_redis
from app.core.runtime_health import operational_snapshot
async def main():
 await init_db(create_tables=False);await init_redis()
 data=json.loads(sys.stdin.read());redis=await get_redis()
 await redis.set('vanguard:ops:host',json.dumps(data),ex=900)
 async with get_db_session() as db: result=await operational_snapshot(db)
 print(json.dumps({'business':result}))
 await close_redis();await close_db()
asyncio.run(main())
'''
    output = subprocess.run(['docker', 'exec', '-i', 'vanguard-backend', 'python', '-c', code],
        input=json.dumps(data), capture_output=True, text=True, timeout=40)
    if output.returncode:
        data['alerts'].append({'code': 'business_monitor_unavailable', 'severity': 'critical'})
    else:
        business = json.loads([line for line in output.stdout.splitlines() if line.startswith('{"business":')][-1])
        data['business'] = business['business']
    temp = STATUS.with_suffix('.tmp')
    temp.write_text(json.dumps(data));temp.replace(STATUS)
    signature = lambda x: sorted(json.dumps(item, sort_keys=True) for item in
        x.get('alerts', []) + x.get('business', {}).get('alerts', []))
    if signature(data) != signature(previous):
        print(json.dumps({'event': 'vanguard_alerts_changed', 'alerts': signature(data)}))
    if any('"critical"' in item for item in signature(data)):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
