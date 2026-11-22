"""Server-only R2 release; retain all runtime data and the R1 observation clock.

All production reports stay root-readable on oracle4c24g. No environment,
business data, source archive, or verification payload is exported.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import time
import urllib.request

ROOT = Path('/opt/vanguard/releases/send-result-20261113-r2')
COMPOSE = Path('/opt/shared-postgres/stacks/vanguard/compose.json')
BASE = 'local/vanguard:send-result-20261113-r1'
IMAGE = 'local/vanguard:send-result-20261113-r2'
APPS = ('backend', 'celery-worker', 'celery-beat', 'resource-search-worker',
        'telegram-growth-worker-survival-continuity', 'telegram-guardian-worker', 'reconciliation')
INFRA = ('sub2api-dr-postgres', 'oracle-shared-postgres-gateway', 'vanguard-redis')
PAUSE_KEY = 'growth.send_result.release_pause.r2'


def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs)


def private(path: Path, content: str):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as out:
        out.write(content)


def inspect(name):
    return json.loads(run('docker', 'inspect', name).stdout)[0]


def infrastructure():
    result = {}
    for name in INFRA:
        info = inspect(name)
        result[name] = {'id': info['Id'], 'started_at': info['State']['StartedAt'], 'restarts': info['RestartCount']}
    return result


def prepare():
    assert not (ROOT / 'built.json').exists()
    data = json.loads(COMPOSE.read_text())
    assert all(data['services'][name]['image'] == BASE for name in APPS)
    private(ROOT / 'compose.baseline.json', COMPOSE.read_text())
    private(ROOT / 'infrastructure.before.json', json.dumps(infrastructure()))
    overlay, context = ROOT / 'overlay', ROOT / 'context'
    overlay.mkdir()
    context.mkdir()
    with tarfile.open(ROOT / 'overlay.tar.gz') as archive:
        assert all(not x.name.startswith('/') and '..' not in Path(x.name).parts for x in archive.getmembers())
        archive.extractall(overlay, filter='data')
    container = run('docker', 'create', '--network', 'none', BASE).stdout.strip()
    try:
        for directory in ('app', 'scripts'):
            run('docker', 'cp', f'{container}:/app/{directory}', str(context / directory))
    finally:
        run('docker', 'rm', container)
    # Normalize line endings before strict incremental patches, as in R1.
    for source in context.rglob('*.py'):
        source.write_text(source.read_text())
    for patch in sorted((overlay / 'patches').glob('*.patch')):
        applied = run('patch', '--batch', '--forward', '--fuzz=0', '-p1', '-d', str(context), input=patch.read_text())
        private(ROOT / (patch.name + '.log'), applied.stdout + applied.stderr)
    shutil.copytree(overlay / 'tests', context / 'tests')
    shutil.copy2(overlay / 'pyproject.toml', context / 'pyproject.toml')
    shutil.copy2(__file__, context / 'operator_release.py')
    (context / 'Dockerfile').write_text(f'FROM {BASE}\nCOPY --chown=appuser:appgroup app /app/app\nCOPY --chown=appuser:appgroup scripts /app/scripts\nRUN python -m compileall -q /app/app /app/scripts\n')
    (context / 'Dockerfile.test').write_text(f'FROM {IMAGE}\nUSER root\nRUN pip install --disable-pip-version-check "fakeredis[lua]" aiosqlite pytest-asyncio ruff\nCOPY tests /app/tests\nCOPY pyproject.toml /app/pyproject.toml\nCOPY operator_release.py /app/ops/send_result_omissions_release.py\n')
    for suffix, dockerfile in (('', 'Dockerfile'), ('-test', 'Dockerfile.test')):
        with (ROOT / ('build' + suffix + '.log')).open('w') as log:
            subprocess.run(['docker', 'build', '-t', IMAGE + suffix, '-f', str(context / dockerfile), str(context)],
                           stdout=log, stderr=subprocess.STDOUT, check=True)
    private(ROOT / 'built.json', json.dumps({'image': IMAGE, 'image_id': inspect_image(IMAGE)}))


def inspect_image(image):
    return json.loads(run('docker', 'image', 'inspect', image).stdout)[0]['Id']


def test(*, operator_only=False):
    import secrets
    network, database = 'vanguard-send-result-r2-test', 'vanguard-send-result-r2-pg'
    password = secrets.token_urlsafe(24)
    env = ROOT / '.test.env'
    run('docker', 'network', 'create', network)
    try:
        run('docker', 'run', '-d', '--rm', '--network', network, '--name', database, '--memory', '512m',
            '--label', 'vanguard.send-result-test=true', '-e', 'POSTGRES_DB=vanguard_growth_test',
            '-e', 'POSTGRES_PASSWORD=' + password, 'postgres:18-alpine')
        for _ in range(30):
            if subprocess.run(['docker', 'exec', database, 'pg_isready', '-U', 'postgres'], capture_output=True).returncode == 0:
                break
            time.sleep(1)
        url = f'postgresql+asyncpg://postgres:{password}@{database}:5432/vanguard_growth_test'
        private(env, f'VANGUARD_TEST_POSTGRES_URL={url}\nDATABASE_URL={url}\nDEBUG=true\nVANGUARD_TEST_RELEASE_SCRIPT=/app/ops/send_result_omissions_release.py\n')
        args = ['tests/unit/test_send_result_policy.py', 'tests/unit/test_send_result_omissions.py',
                'tests/unit/test_simplified_growth.py', 'tests/unit/test_growth_remediation.py',
                'tests/unit/test_growth_event_inbox.py', 'tests/unit/test_growth_dispatch_boundaries.py',
                'tests/unit/test_production_diagnosis_regressions.py', 'tests/integration/test_simplified_growth_postgres.py',
                '--override-ini', 'addopts=', '-q', '--disable-warnings', '--tb=short', '--maxfail=5']
        code = 'from tests.conftest import _import_models;_import_models();import pytest,sys;sys.exit(pytest.main(' + repr(args) + '))'
        if operator_only:
            args = ['tests/integration/test_simplified_growth_postgres.py::test_release_resume_locks_only_config_and_preserves_new_pause',
                    '--override-ini', 'addopts=', '-q', '--disable-warnings', '--tb=short']
            code = 'from tests.conftest import _import_models;_import_models();import pytest,sys;sys.exit(pytest.main(' + repr(args) + '))'
        with (ROOT / ('operator-tests.log' if operator_only else 'candidate-tests.log')).open('w') as log:
            subprocess.run(['docker', 'run', '--rm', '--network', network, '--memory', '1024m',
                '--env-file', str(env), '--entrypoint', 'python', IMAGE + '-test', '-c', code],
                stdout=log, stderr=subprocess.STDOUT, check=True)
        if operator_only:
            import hashlib
            private(ROOT / 'operator-tests-passed.json', json.dumps({'passed': True, 'image_id': inspect_image(IMAGE),
                'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}))
            return
        # Unit-test alert evaluation separately from production Prometheus.
        prometheus_image = inspect('vanguard-prometheus')['Config']['Image']
        with (ROOT / 'alert-tests.log').open('w') as log:
            subprocess.run(['docker', 'run', '--rm', '--user', '0', '--network', 'none', '--entrypoint', '/bin/promtool',
                '-v', str(ROOT / 'overlay/monitoring') + ':/rules:ro', '-w', '/rules', prometheus_image,
                'test', 'rules', 'send-result-rules.test.yml'], stdout=log, stderr=subprocess.STDOUT, check=True)
        private(ROOT / 'tests-passed.json', json.dumps({'passed': True, 'image_id': inspect_image(IMAGE)}))
    finally:
        env.unlink(missing_ok=True)
        subprocess.run(['docker', 'rm', '-f', database], capture_output=True)
        subprocess.run(['docker', 'network', 'rm', network], capture_output=True)


def helper(data, code):
    env = ROOT / '.helper.env'
    private(env, ''.join(f'{key}={value}\n' for key, value in data['services']['backend']['environment'].items()))
    network = next(iter(data['services']['backend']['networks']))
    bootstrap = ('import app.main;from app.core import database;from app.core.config import settings;'
        'from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker;'
        'database.async_session_factory=async_sessionmaker(create_async_engine(settings.DATABASE_URL,pool_size=2,max_overflow=1),expire_on_commit=False,autoflush=False);')
    try:
        return run('docker', 'run', '--rm', '--network', network, '--user', '0', '--env-file', str(env),
                   '-v', f'{ROOT}:/release', '--entrypoint', 'python', IMAGE, '-c', bootstrap + code)
    except subprocess.CalledProcessError as exc:
        private(ROOT / 'helper.failure.log', (exc.stdout or '') + (exc.stderr or ''))
        raise
    finally:
        env.unlink(missing_ok=True)


PAUSE = '''import asyncio,json,os
from datetime import datetime
from sqlalchemy import select
from app.core.database import get_db_session
from app.core.account.models import AccountOperationConfig,TelegramAccount,AccountType
from app.core.settings_models import SystemSetting
async def work():
 async with get_db_session() as db:
  key='growth.send_result.release_pause.r2'
  marker=await db.get(SystemSetting,key,with_for_update=True)
  if marker:
   assert not json.loads(marker.value).get('resumed_at')
   return
  rows=list((await db.scalars(select(AccountOperationConfig).join(TelegramAccount,TelegramAccount.id==AccountOperationConfig.account_id).where(TelegramAccount.account_type==AccountType.PROMOTER))).all())
  clock=await db.get(SystemSetting,'growth.canary.account.2')
  operations=[{'id':c.id,'enabled':c.enabled} for c in rows]
  for c in rows:c.enabled=False
  await db.flush()
  for entry,c in zip(operations,rows):entry['paused_updated_at']=c.updated_at.isoformat()
  state={'at':datetime.utcnow().isoformat(),'operations':operations,'observation_before':json.loads(clock.value)}
  db.add(SystemSetting(key=key,value=json.dumps(state)))
  path='/release/pause.json'
  fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
  with os.fdopen(fd,'w') as out:json.dump(state,out)
  await db.commit()
asyncio.run(work())'''

RESUME = '''import asyncio,json
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.orm import lazyload
from app.core.database import get_db_session
from app.core.account.models import AccountOperationConfig
from app.core.settings_models import SystemSetting
async def work():
 async with get_db_session() as db:
  marker=await db.get(SystemSetting,'growth.send_result.release_pause.r2',with_for_update=True)
  state=json.loads(marker.value)
  if state.get('resumed_at'):return
  clock=await db.get(SystemSetting,'growth.canary.account.2')
  assert json.loads(clock.value)['send_result_started_at']==state['observation_before']['send_result_started_at']
  for item in state['operations']:
   cfg=await db.scalar(select(AccountOperationConfig).options(lazyload('*')).where(AccountOperationConfig.id==item['id']).with_for_update(of=AccountOperationConfig))
   if cfg and cfg.enabled is False and cfg.updated_at.isoformat()==item['paused_updated_at']:
    cfg.enabled=item['enabled']
  state['resumed_at']=datetime.utcnow().isoformat()
  marker.value=json.dumps(state)
  await db.commit()
asyncio.run(work())'''

IDLE = '''import asyncio,json
from sqlalchemy import text
from app.core.database import get_db_session
async def work():
 async with get_db_session() as db:
  sending=await db.scalar(text("SELECT count(*) FROM ad_delivery_log WHERE status='sending'"))
  leases=await db.scalar(text("SELECT count(*) FROM account_outbound_attempt WHERE state IN ('reserved','attempted') AND lease_expires_at>now()"))
  assert sending==0 and leases==0
asyncio.run(work())'''


def publish():
    assert not (ROOT / 'published.json').exists()
    assert json.loads((ROOT / 'tests-passed.json').read_text())['image_id'] == inspect_image(IMAGE)
    data = json.loads(COMPOSE.read_text())
    assert data == json.loads((ROOT / 'compose.baseline.json').read_text())
    helper(data, PAUSE)
    for service in ('celery-beat', 'celery-worker', 'telegram-growth-worker-survival-continuity', 'reconciliation'):
        result = run('docker', 'compose', '-f', str(COMPOSE), 'stop', '-t', '120', service)
        private(ROOT / (service + '.stop.log'), result.stdout + result.stderr)
        info = inspect(data['services'][service]['container_name'])
        assert not info['State']['OOMKilled']
        if info['State']['ExitCode'] == 137:
            # The deployed reconciliation launcher does not forward TERM.
            assert service == 'reconciliation'
            helper(data, IDLE)
            private(ROOT / 'reconciliation_shutdown_review.json', json.dumps({
                'container_id': info['Id'], 'finished_at': info['State']['FinishedAt'], 'proved_idle': True}))
    helper(data, IDLE)
    updated = json.loads(json.dumps(data))
    for name in APPS:
        updated['services'][name]['image'] = IMAGE
    for name in data['services']:
        if name not in APPS:
            assert updated['services'][name] == data['services'][name]
        else:
            old, new = data['services'][name].copy(), updated['services'][name].copy()
            old.pop('image'); new.pop('image')
            assert old == new
    temporary = COMPOSE.with_suffix('.r2.json')
    private(temporary, json.dumps(updated, indent=2))
    run('docker', 'compose', '-f', str(temporary), 'config', '--quiet')
    temporary.replace(COMPOSE)
    for name in ('backend', 'telegram-guardian-worker', 'resource-search-worker', 'reconciliation',
                 'telegram-growth-worker-survival-continuity', 'celery-worker', 'celery-beat'):
        result = run('docker', 'compose', '-f', str(COMPOSE), 'up', '-d', '--no-deps', name)
        private(ROOT / (name + '.up.log'), result.stdout + result.stderr)
    helper(updated, RESUME)
    install_alerts()
    private(ROOT / 'published.json', json.dumps({'at': datetime.now(timezone.utc).isoformat(), 'image_id': inspect_image(IMAGE)}))


def install_alerts():
    prometheus = inspect('vanguard-prometheus')
    mount = next(m for m in prometheus['Mounts'] if m['Destination'] == '/etc/prometheus/alerts.yml')
    path = Path(mount['Source'])
    before = path.read_text()
    private(ROOT / 'alerts.before.yml', before)
    candidate = (ROOT / 'overlay/monitoring/alerts.yml').read_text()
    group = '\n  - name: vanguard-send-result\n' + candidate.split('\n  - name: vanguard-send-result\n', 1)[1]
    assert 'name: vanguard-send-result' not in before
    after = before.rstrip() + '\n' + group
    for name in ('VanguardAdUnknownWarning', 'VanguardAdUnknownCritical', 'VanguardSurvivalOverdue'):
        pattern = r'(- alert: ' + name + r'\s+expr: )([^\n]+)'
        def guard(match):
            expression = match[2]
            if 'vanguard_send_result_survival_enabled' in expression:
                return match[0]
            return match[1] + '(' + expression + ') unless on() (vanguard_send_result_survival_enabled == 0)'
        after, count = re.subn(pattern, guard, after)
        assert count <= 1
    # An existing malformed selector must not invalidate the new rule group.
    after = after.replace('vanguard_p4_pacing_decisions_total[reason="eligible"]}',
                          'vanguard_p4_pacing_decisions_total{reason="eligible"}')
    private(ROOT / 'alerts.candidate.yml', after)
    run('docker', 'run', '--rm', '--user', '0', '--network', 'none', '--entrypoint', '/bin/promtool',
        '-v', f'{ROOT}/alerts.candidate.yml:/rules.yml:ro', prometheus['Config']['Image'], 'check', 'rules', '/rules.yml')
    # Keep the existing bind-mount inode, ownership and mode.
    with path.open('w') as out:
        out.write(after)
    try:
        run('docker', 'kill', '--signal', 'HUP', 'vanguard-prometheus')
    except BaseException:
        path.write_text(before)
        run('docker', 'kill', '--signal', 'HUP', 'vanguard-prometheus')
        raise


VERIFY = '''import asyncio,json,os,urllib.request,urllib.error
from app.core.security import create_access_token
from app.core.config import settings
import asyncpg
async def work():
 c=await asyncpg.connect(os.environ['DATABASE_URL'].replace('postgresql+asyncpg:','postgresql:'),server_settings={'default_transaction_read_only':'on','statement_timeout':'5000'})
 try:
  assert await c.fetchval('SELECT current_database()')=='vanguard'
  uid=await c.fetchval("SELECT id FROM admin_user WHERE is_active AND role='admin' ORDER BY id LIMIT 1")
  token=create_access_token({'sub':str(uid)})
  def request(path,method='GET',scrape=False):
   req=urllib.request.Request('http://127.0.0.1:8000'+path,headers={'Authorization':'Bearer '+(settings.METRICS_SCRAPE_TOKEN if scrape else token),'Content-Type':'application/json'},method=method,data=b'{}' if method=='POST' else None)
   try:
    with urllib.request.urlopen(req,timeout=20) as r:return r.status,r.read()
   except urllib.error.HTTPError as e:return e.code,b''
  assert request('/health')[0]==200
  assert request('/api/automation/ads/delivery-execution')[0]==200
  assert request('/api/automation/ads/survival-check/run','POST')[0]==410
  status,body=request('/metrics',scrape=True)
  assert status==200
  for metric in (b'vanguard_send_result_survival_enabled 0.0',b'vanguard_send_result_inbox_oldest_age_seconds',b'vanguard_send_result_sending_oldest_age_seconds'):
   assert metric in body
  pause=json.loads(await c.fetchval("SELECT value FROM system_setting WHERE key='growth.send_result.release_pause.r2'"))
  canary=json.loads(await c.fetchval("SELECT value FROM system_setting WHERE key='growth.canary.account.2'"))
  assert pause['resumed_at'] and canary['send_result_started_at']==pause['observation_before']['send_result_started_at']
  for item in pause['operations']:
   assert await c.fetchval('SELECT enabled FROM telegram_account_operation_config WHERE id=$1',item['id'])==item['enabled']
  assert await c.fetchval('SELECT count(*) FROM group_ad_frequency WHERE daily_review_due_at IS NOT NULL')==0
  assert await c.fetchval('SELECT count(*) FROM telegram_account WHERE id=31')==0
  assert not await c.fetchval('SELECT enabled FROM telegram_account_operation_config WHERE account_id=4')
  print(json.dumps({'verified':True,'observation_start_preserved':True,'automatic_test_ads':False}))
 finally:await c.close()
asyncio.run(work())'''


def verify():
    image_id = inspect_image(IMAGE)
    data = json.loads(COMPOSE.read_text())
    for _ in range(36):
        if all(inspect(data['services'][n]['container_name'])['State']['Running'] for n in APPS):
            backend = inspect('vanguard-backend')
            if backend['State'].get('Health', {}).get('Status') == 'healthy':
                break
        time.sleep(5)
    # Aggregation refresh is 60s, independent of readiness.
    time.sleep(5)
    for _ in range(20):
        result = subprocess.run(['docker', 'exec', '-i', 'vanguard-backend', 'python', '-'],
                                input=VERIFY, text=True, capture_output=True)
        if result.returncode == 0:
            break
        time.sleep(5)
    private(ROOT / 'verification.log', result.stdout + result.stderr)
    assert result.returncode == 0
    services = {}
    for name in APPS:
        info = inspect(data['services'][name]['container_name'])
        assert info['Image'] == image_id and info['State']['Running'] and not info['State']['OOMKilled']
        services[name] = {'id': info['Id'], 'started_at': info['State']['StartedAt'],
                          'restarts': info['RestartCount'], 'health': info['State'].get('Health', {}).get('Status')}
    assert infrastructure() == json.loads((ROOT / 'infrastructure.before.json').read_text())
    request = urllib.request.Request('https://vanguard.pipenai.xyz/health', headers={'User-Agent': 'Mozilla/5.0'})
    assert urllib.request.urlopen(request, timeout=20).status == 200
    prometheus = inspect('vanguard-prometheus')
    address = next(iter(prometheus['NetworkSettings']['Networks'].values()))['IPAddress']
    base = f'http://{address}:9090/monitoring/prometheus/api/v1/'
    targets = json.load(urllib.request.urlopen(base + 'targets', timeout=15))
    assert len(targets['data']['activeTargets']) >= 7
    assert all(t['health'] == 'up' for t in targets['data']['activeTargets'])
    group = None
    for _ in range(18):
        rules = json.load(urllib.request.urlopen(base + 'rules', timeout=15))
        group = next((g for g in rules['data']['groups'] if g['name'] == 'vanguard-send-result'), None)
        if group and len(group['rules']) == 3 and all(r['health'] == 'ok' for r in group['rules']):
            break
        time.sleep(5)
    assert group is not None
    assert len(group['rules']) == 3 and all(r['health'] == 'ok' for r in group['rules'])
    private(ROOT / 'verification.json', json.dumps({'verified': True, 'services': services,
        'observation_start_preserved': True, 'infrastructure_unchanged': True,
        'prometheus_targets_up': len(targets['data']['activeTargets']), 'alert_group': group}, default=str))


def rollback():
    data = json.loads(COMPOSE.read_text())
    pause = PAUSE.replace('growth.send_result.release_pause.r2', 'growth.send_result.release_rollback.r2')
    pause = pause.replace('/release/pause.json', '/release/rollback-pause.json')
    helper(data, pause)
    for service in ('celery-beat', 'celery-worker', 'telegram-growth-worker-survival-continuity', 'reconciliation'):
        run('docker', 'compose', '-f', str(COMPOSE), 'stop', '-t', '120', service)
    baseline = json.loads((ROOT / 'compose.baseline.json').read_text())
    restored = json.loads(json.dumps(data))
    for name in APPS:
        restored['services'][name]['image'] = baseline['services'][name]['image']
    temporary = COMPOSE.with_suffix('.r2-rollback.json')
    private(temporary, json.dumps(restored, indent=2))
    run('docker', 'compose', '-f', str(temporary), 'config', '--quiet')
    temporary.replace(COMPOSE)
    run('docker', 'compose', '-f', str(COMPOSE), 'up', '-d', '--no-deps', *APPS)
    private(ROOT / 'rollback.json', json.dumps({'at': datetime.now(timezone.utc).isoformat(),
        'promotion_remains_paused': True, 'database_restored': False, 'history_replayed': False}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['prepare', 'test', 'test-operator', 'publish', 'verify', 'rollback'])
    phase = parser.parse_args().phase
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.umask(0o077)
    (ROOT / (phase + '.error.json')).unlink(missing_ok=True)
    try:
        if phase == 'test-operator':
            test(operator_only=True)
        else:
            globals()[phase]()
    except BaseException as exc:
        private(ROOT / (phase + '.error.json'), json.dumps({'exception': type(exc).__name__, 'phase': phase}))
        raise
    private(ROOT / (phase + '.done.json'), json.dumps({'at': datetime.now(timezone.utc).isoformat()}))


if __name__ == '__main__':
    main()
