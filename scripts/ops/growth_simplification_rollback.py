"""Pause growth and restore application images from a verified phase checkpoint."""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COMPOSE = Path('/opt/shared-postgres/stacks/vanguard/compose.json')
GROWTH = 'telegram-growth-worker-survival-continuity'
APPS = ('backend', 'telegram-guardian-worker', 'resource-search-worker',
        'reconciliation', GROWTH, 'celery-worker', 'frontend', 'celery-beat')
PAUSE = r'''
import asyncio,json,os
import asyncpg
async def main():
 c=await asyncpg.connect(os.environ['DATABASE_URL'].replace('postgresql+asyncpg:', 'postgresql:'),timeout=15)
 keys=['automation.auto_join_scheduler','automation.ad_delivery_execution','automation.group_qualification']
 marker='growth.writable_v2.rollback_pause'
 async with c.transaction():
  backup=await c.fetchval('select value from system_setting where key=$1',marker)
  if not backup:
   previous={k:await c.fetchval('select value from system_setting where key=$1 for update',k) for k in keys}
   await c.execute('insert into system_setting(key,value) values($1,$2)',marker,json.dumps(previous))
  for k in keys:
   raw=await c.fetchval('select value from system_setting where key=$1 for update',k)
   value=json.loads(raw or '{}')
   value['enabled']=False
   if k.endswith('group_qualification'):
    value.update(execute_reviews=False,execute_verification=False,execute_exits=False)
   await c.execute('insert into system_setting(key,value) values($1,$2) on conflict(key) do update set value=excluded.value,updated_at=now()',k,json.dumps(value))
 await c.close()
 print('growth paused; original switches preserved in rollback marker')
asyncio.run(main())
'''
STOP_BEAT = r'''
import os,signal
from pathlib import Path
for p in Path('/proc').iterdir():
 if not p.name.isdigit(): continue
 try: cmd=(p/'cmdline').read_bytes().split(b'\0')
 except OSError: continue
 if b'/usr/local/bin/celery' in cmd and b'beat' in cmd and b'app.celery' in cmd:
  os.kill(int(p.name),signal.SIGTERM)
'''


def run(*args: str, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=('p0', 'p1'), default='p0')
    phase = parser.parse_args().phase
    current = json.loads(COMPOSE.read_text())
    previous = json.loads((ROOT / f'compose.before-{phase}.json').read_text())
    for name in set(current['services']) - set(APPS):
        assert current['services'][name] == previous['services'][name]
    run('docker', 'exec', '-i', 'vanguard-backend', 'python', '-', input=PAUSE, text=True)
    run('docker', 'exec', '-i', 'vanguard-celery-beat', 'python', '-', input=STOP_BEAT, text=True)
    run('docker', 'compose', '-f', str(COMPOSE), 'stop', '-t', '600', 'celery-beat', 'celery-worker')
    container = current['services'][GROWTH]['container_name']
    signal = subprocess.check_output(['docker', 'exec', container, 'python', '-c',
        "from pathlib import Path; print('SIGTERM' if '_stop_requested' in Path('/app/app/workers/telegram_worker.py').read_text() else 'SIGINT')"], text=True).strip()
    run('docker', 'kill', '--signal=' + signal, container)
    run('docker', 'compose', '-f', str(COMPOSE), 'stop', '-t', '600', GROWTH)
    state = json.loads(subprocess.check_output(['docker', 'inspect', current['services'][GROWTH]['container_name']]))[0]['State']
    if state['Running'] or state['OOMKilled'] or state['ExitCode'] == 137:
        raise RuntimeError('growth did not finish cleanup; leave growth paused')
    temp = COMPOSE.with_suffix('.rollback.json')
    temp.write_text(json.dumps(previous, indent=2))
    temp.chmod(0o600)
    run('docker', 'compose', '-f', str(temp), 'config', '--quiet')
    temp.replace(COMPOSE)
    for name in APPS:
        run('docker', 'compose', '-f', str(COMPOSE), 'up', '-d', '--no-deps', name)
    (ROOT / f'{phase}-rolled-back.json').write_text(json.dumps({
        'at': datetime.now(UTC).isoformat(), 'reason': 'database_connection_slots_exhausted',
        'growth': 'paused', 'schema': '077 retained', 'business_data': 'retained'}))
    print('applications restored; growth remains paused', flush=True)


if __name__ == '__main__':
    main()
