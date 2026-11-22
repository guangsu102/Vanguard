"""Application-only publish, verification and rollback on oracle4c24g."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time

ROOT = Path('/opt/vanguard/releases/send-result-20261113-r1')
COMPOSE = Path('/opt/shared-postgres/stacks/vanguard/compose.json')
IMAGE = 'local/vanguard:send-result-20261113-r1'
APPS = ('backend', 'celery-worker', 'celery-beat', 'resource-search-worker',
        'telegram-growth-worker-survival-continuity', 'telegram-guardian-worker', 'reconciliation')


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as out:
        out.write(data)


def helper(data, *args, network=True):
    env = dict(data['services']['backend']['environment'])
    env.update(GROWTH_SIMPLIFIED_ENABLED='true', TELEGRAM_RPC_PACING_ENABLED='true')
    path = ROOT / '.helper.env'
    private(path, ''.join(f'{k}={v}\n' for k, v in env.items()))
    network_name = next(iter(data['services']['backend']['networks'])) if network else 'none'
    if args[0] == '-c':
        bootstrap = 'import app.main; from app.core import database; from app.core.config import settings; from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker; database.async_session_factory=async_sessionmaker(create_async_engine(settings.DATABASE_URL,pool_size=2,max_overflow=1),expire_on_commit=False,autoflush=False);'
        args = ('-c', bootstrap + args[1])
    try:
        return run('docker', 'run', '--rm', '--user', '0', '--network', network_name, '--env-file', str(path),
                   '-v', f'{ROOT}:/release', '--entrypoint', 'python', IMAGE, *args,
                   capture_output=True, text=True)
    finally:
        path.unlink(missing_ok=True)


PAUSE = '''import asyncio,json
from sqlalchemy import select
from app.core.database import get_db_session
from app.core.account.models import AccountOperationConfig,TelegramAccount,AccountType
from app.core.settings_models import SystemSetting
async def main():
 async with get_db_session() as db:
  key='growth.send_result.release_pause.v1'
  if await db.get(SystemSetting,key):return
  rows=list((await db.scalars(select(AccountOperationConfig).join(TelegramAccount,TelegramAccount.id==AccountOperationConfig.account_id).where(TelegramAccount.account_type==AccountType.PROMOTER))).all())
  before=[{'id':c.id,'enabled':c.enabled} for c in rows]
  db.add(SystemSetting(key=key,value=json.dumps({'operations':before})))
  for c in rows:c.enabled=False
  await db.commit()
 print('promotion_paused')
asyncio.run(main())'''


def publish():
    if not (ROOT / 'tests-passed.json').exists():
        raise RuntimeError('candidate_tests_required')
    tested = json.loads((ROOT / 'tests-passed.json').read_text())
    if tested.get('image_id') != json.loads(subprocess.check_output(['docker','image','inspect',IMAGE]))[0]['Id']:
        raise RuntimeError('candidate_image_changed_after_test')
    if (ROOT / 'published.json').exists():
        raise RuntimeError('already_published')
    data = json.loads(COMPOSE.read_text())
    baseline = json.loads((ROOT / 'compose.baseline.json').read_text())
    if data != baseline:
        raise RuntimeError('production_compose_changed_since_baseline')
    helper(data, '-c', 'import app.main; from app.core.scheduler.growth_dispatch import STAGES; assert "exit" in STAGES and "daily_survival_and_exit" not in STAGES; assert any(getattr(r,"path",None)=="/metrics" for r in app.main.app.routes); print("candidate_import_ok")', network=False)
    infra = {}
    for name in ('sub2api-dr-postgres', 'oracle-shared-postgres-gateway', 'vanguard-redis'):
        info = json.loads(subprocess.check_output(['docker', 'inspect', name]))[0]
        infra[name] = {'id': info['Id'], 'started_at': info['State']['StartedAt'], 'restarts': info['RestartCount']}
    private(ROOT / 'infrastructure.before.json', json.dumps(infra))
    print(helper(data, '-m', 'scripts.activate_send_result').stdout, flush=True)
    print(helper(data, '-c', PAUSE).stdout, flush=True)
    # No new claims; shutdown allows current owner RPCs to finish and save receipts.
    for name in ('celery-beat', 'celery-worker', 'telegram-growth-worker-survival-continuity', 'reconciliation'):
        run('docker', 'compose', '-f', str(COMPOSE), 'stop', '-t', '120', name)
        info = json.loads(subprocess.check_output(['docker', 'inspect', data['services'][name]['container_name']]))[0]
        reviewed = False
        review = ROOT / 'reconciliation_shutdown_review.json'
        if name == 'reconciliation' and review.exists():
            proof = json.loads(review.read_text())
            reviewed = (proof.get('proved_idle') and proof.get('container_id') == info['Id']
                        and proof.get('finished_at') == info['State']['FinishedAt'])
        if info['State']['OOMKilled'] or (info['State']['ExitCode'] == 137 and not reviewed):
            raise RuntimeError('unclean_shutdown:' + name)
    print(helper(data, '-m', 'scripts.activate_send_result', '--apply', '--snapshot', '/release/cutover-data.json').stdout, flush=True)
    # Exact service configuration is retained except image and matching feature flags.
    updated = json.loads(json.dumps(data))
    for name in APPS:
        updated['services'][name]['image'] = IMAGE
        updated['services'][name]['environment'].update(GROWTH_SIMPLIFIED_ENABLED='true', TELEGRAM_RPC_PACING_ENABLED='true')
    updated['services']['frontend']['image'] = IMAGE + '-frontend'
    for name in set(data['services']) - set(APPS) - {'frontend'}:
        assert data['services'][name] == updated['services'][name]
    temporary = COMPOSE.with_suffix('.send-result.json')
    private(temporary, json.dumps(updated, indent=2))
    run('docker', 'compose', '-f', str(temporary), 'config', '--quiet')
    temporary.replace(COMPOSE)
    monitor = Path('/opt/vanguard/scripts/ops/runtime_monitor.py')
    if monitor.exists() and 'vanguard:growth_dispatch:v1:' in monitor.read_text():
        private(ROOT / 'runtime_monitor.before.py', monitor.read_text())
        monitor.write_text(monitor.read_text().replace('vanguard:growth_dispatch:v1:', 'vanguard:growth_dispatch:v2:'))
    for name in ('backend', 'telegram-guardian-worker', 'resource-search-worker', 'reconciliation',
                 'telegram-growth-worker-survival-continuity', 'celery-worker', 'frontend', 'celery-beat'):
        run('docker', 'compose', '-f', str(COMPOSE), 'up', '-d', '--no-deps', name)
    print(helper(updated, '-m', 'scripts.activate_send_result', '--resume').stdout, flush=True)
    private(ROOT / 'published.json', json.dumps({'started_at': datetime.now(timezone.utc).isoformat(), 'image': IMAGE}))


def rollback():
    data = json.loads(COMPOSE.read_text())
    # Stop automatic promotion and exits before restoring older executables.
    pause = PAUSE.replace("if await db.get(SystemSetting,key):return", "pass").replace("db.add(SystemSetting(key=key,value=json.dumps({'operations':before})))", "pass")
    helper(data, '-c', pause)
    monitor_before = ROOT / 'runtime_monitor.before.py'
    if monitor_before.exists():
        Path('/opt/vanguard/scripts/ops/runtime_monitor.py').write_text(monitor_before.read_text())
    baseline = json.loads((ROOT / 'compose.baseline.json').read_text())
    restored = json.loads(json.dumps(data))
    for name in (*APPS, 'frontend'):
        restored['services'][name] = baseline['services'][name]
    private(COMPOSE, json.dumps(restored, indent=2))
    run('docker', 'compose', '-f', str(COMPOSE), 'up', '-d', '--no-deps', *APPS, 'frontend')
    print('application_images_restored_promotion_remains_paused')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['preview', 'publish', 'rollback', 'observe', 'allow-rejoin', 'retry-exit'])
    parser.add_argument("--account", type=int)
    parser.add_argument("--membership", type=int)
    args = parser.parse_args()
    if args.phase == 'publish':
        publish()
    elif args.phase == 'rollback':
        rollback()
    elif args.phase in {'observe', 'allow-rejoin', 'retry-exit'}:
        target = args.account if args.phase == 'observe' else args.membership
        if not target:
            raise ValueError('account_or_membership_required')
        flag = '--begin-observation' if args.phase == 'observe' else '--' + args.phase
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
        print(helper(json.loads(COMPOSE.read_text()), '-m', 'scripts.activate_send_result', flag, str(target),
                     '--snapshot', f'/release/manual-{args.phase}-{target}-{stamp}.json').stdout)
    else:
        print(helper(json.loads(COMPOSE.read_text()), '-m', 'scripts.activate_send_result').stdout)


if __name__ == '__main__':
    main()
