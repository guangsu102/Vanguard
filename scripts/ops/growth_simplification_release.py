"""Run on oracle4c24g: bounded application-only staged release using canonical Compose.

The build context and this script live in a dedicated release directory. Runtime
configuration backups and temporary environment files stay root-readable there.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

COMPOSE = Path('/opt/shared-postgres/stacks/vanguard/compose.json')
GROWTH = 'telegram-growth-worker-survival-continuity'
APPS = ('backend', 'celery-worker', 'celery-beat', 'resource-search-worker',
        GROWTH, 'telegram-guardian-worker', 'reconciliation')
ROOT = Path(__file__).resolve().parent
IMAGE = 'local/vanguard:writable-v2-20261107-r2'
FRONTEND = IMAGE + '-frontend'


def run(*args: str, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def load() -> dict:
    data = json.loads(COMPOSE.read_text())
    assert set(APPS) <= data['services'].keys()
    for name in APPS:
        env = data['services'][name]['environment']
        assert int(env['DATABASE_POOL_SIZE']) == 2
        assert int(env['DATABASE_MAX_OVERFLOW']) <= 1
        assert 'oracle-shared-postgres:5432/vanguard' in env['DATABASE_URL']
    return data


def compose(*args: str):
    return run('docker', 'compose', '-f', str(COMPOSE), *args)


def helper(data: dict, *args: str, simplified: bool = False, network: bool = True):
    env = dict(data['services']['backend']['environment'])
    env.update(TELEGRAM_RPC_PACING_ENABLED='true',
               GROWTH_SIMPLIFIED_ENABLED=str(simplified).lower(), DEBUG='false')
    path = ROOT / '.helper.env'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'w') as out:
            for key, value in env.items():
                if '\n' in str(value):
                    raise ValueError('multiline environment is unsupported')
                out.write(f'{key}={value}\n')
        network_name = next(iter(data['services']['backend']['networks'])) if network else 'none'
        return run('docker', 'run', '--rm', '--network', network_name,
                   '--env-file', str(path), '--entrypoint', 'python', IMAGE, *args)
    finally:
        path.unlink(missing_ok=True)


def checkpoint(phase: str, data: dict):
    path = ROOT / f'compose.before-{phase}.json'
    if path.exists():
        raise RuntimeError(f'phase already started: {phase}; inspect its checkpoint first')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(data, out, indent=2)


def switch(phase: str):
    data = load()
    checkpoint(phase, data)
    pause_marker = f'growth.writable_v2.cutover_pause.{IMAGE.rsplit(":", 1)[-1]}.{phase}'
    if phase == 'p1':
        from rollback import PAUSE
        run('docker', 'exec', '-i', 'vanguard-backend', 'python', '-',
            input=PAUSE.replace('growth.writable_v2.rollback_pause', pause_marker), text=True)
    # Stop claim production first, then let workers finish their in-flight jobs.
    # Existing non-growth queues and Redis state are not purged.
    # Older launchers kept Beat behind os.system and did not forward TERM.
    from rollback import STOP_BEAT
    run('docker', 'exec', '-i', data['services']['celery-beat']['container_name'],
        'python', '-', input=STOP_BEAT, text=True)
    compose('stop', '-t', '600', 'celery-beat', 'celery-worker')
    container = data['services'][GROWTH]['container_name']
    signal = subprocess.check_output(['docker', 'exec', container, 'python', '-c',
        "from pathlib import Path; print('SIGTERM' if '_stop_requested' in Path('/app/app/workers/telegram_worker.py').read_text() else 'SIGINT')"], text=True).strip()
    run('docker', 'kill', '--signal=' + signal, container)
    compose('stop', '-t', '600', GROWTH)
    for name in ('celery-worker', GROWTH):
        container = data['services'][name]['container_name']
        info = json.loads(subprocess.check_output(['docker', 'inspect', container]))[0]
        if info['State']['Running'] or info['State']['OOMKilled'] or info['State']['ExitCode'] == 137:
            raise RuntimeError(f'{name} did not drain cleanly; do not activate the new loop')
    if phase == 'p1':
        helper(data, '-m', 'scripts.activate_simple_growth', '--apply', simplified=True)
    updated = json.loads(json.dumps(data))
    for name in APPS:
        service = updated['services'][name]
        service['image'] = IMAGE
        service['environment'].update(TELEGRAM_RPC_PACING_ENABLED='true',
                                      TELEGRAM_RPC_MIN_INTERVAL_SECONDS='1',
                                      GROWTH_SIMPLIFIED_ENABLED=str(phase == 'p1').lower())
    updated['services']['frontend']['image'] = FRONTEND
    # Assert that every non-application service is byte-for-byte equivalent as JSON.
    for name in set(data['services']) - set(APPS) - {'frontend'}:
        assert data['services'][name] == updated['services'][name]
    temporary = COMPOSE.with_suffix('.growth-new.json')
    temporary.write_text(json.dumps(updated, indent=2))
    temporary.chmod(0o600)
    run('docker', 'compose', '-f', str(temporary), 'config', '--quiet')
    temporary.replace(COMPOSE)
    for name in ('backend', 'telegram-guardian-worker', 'resource-search-worker',
                 'reconciliation', GROWTH, 'celery-worker', 'frontend', 'celery-beat'):
        compose('up', '-d', '--no-deps', name)
    if phase == 'p1':
        from resume import RESTORE
        run('docker', 'exec', '-i', 'vanguard-backend', 'python', '-',
            input=RESTORE.replace('growth.writable_v2.rollback_pause', pause_marker), text=True)
    (ROOT / f'{phase}-started.json').write_text(json.dumps({
        'started_at': datetime.now(UTC).isoformat(), 'image': IMAGE,
        'compose_sha256': hashlib.sha256(COMPOSE.read_bytes()).hexdigest()}))
    print(f'{phase} applications switched; business acceptance is still pending', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=('build', 'expand', 'p0', 'p1', 'status'))
    phase = parser.parse_args().phase
    data = load()
    if phase == 'build':
        base = data['services'][GROWTH]['image']
        (ROOT / 'Dockerfile').write_text(f'FROM {base}\nCOPY --chown=appuser:appgroup app /app/app\n'
            'COPY --chown=appuser:appgroup scripts /app/scripts\n'
            'COPY --chown=appuser:appgroup migrations /app/migrations\n'
            'RUN python -m compileall -q /app/app\n')
        front_base = data['services']['frontend']['image']
        (ROOT / 'Dockerfile.frontend').write_text(f'FROM {front_base}\nCOPY dist /usr/share/nginx/html\n')
        run('docker', 'build', '--network=none', '-t', IMAGE, str(ROOT))
        run('docker', 'build', '--network=none', '-t', FRONTEND, '-f', str(ROOT/'Dockerfile.frontend'), str(ROOT))
        helper(data, '-c', 'import app.main; from app.core.scheduler.growth_dispatch import STAGES; print(list(STAGES))', network=False)
        helper(data, '-c', 'import app.main; from app.core.scheduler.growth_dispatch import STAGES; print(list(STAGES))', simplified=True, network=False)
    elif phase == 'expand':
        helper(data, '-m', 'scripts.apply_sql_migrations', '--files', '077_growth_permissions.sql')
    elif phase in ('p0', 'p1'):
        switch(phase)
    else:
        print(json.dumps({name: data['services'][name]['image'] for name in (*APPS, 'frontend')}))


if __name__ == '__main__':
    main()
