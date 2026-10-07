"""Oracle private backups; PostgreSQL restore drills run in an isolated container."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import time

ROOT = Path('/opt/vanguard')
BACKUPS = ROOT / 'scheduled-backups'
PG = 'sub2api-dr-postgres'
PORT = '55432'
USER = 'vanguard_app'
DATABASE = 'vanguard'


def run(*args, **kwargs):
    return subprocess.run(args, check=True, timeout=300, **kwargs)


def restore_drill(dump):
    name = 'vanguard-restore-drill-' + str(os.getpid())
    run('docker', 'run', '-d', '--name', name, '--network', 'none',
        '--memory', '768m', '--memory-swap', '768m', '--cpus', '1',
        '--tmpfs', '/var/lib/postgresql/data:rw,size=512m',
        '-e', 'PGDATA=/var/lib/postgresql/data', '-e', 'POSTGRES_HOST_AUTH_METHOD=trust',
        '-e', 'POSTGRES_USER=restore_user', '-e', 'POSTGRES_DB=restore_check',
        'postgres:18-alpine', stdout=subprocess.DEVNULL)
    try:
        for _ in range(60):
            ready = subprocess.run(['docker', 'exec', name, 'pg_isready', '-U', 'restore_user',
                                    '-d', 'restore_check'], capture_output=True, timeout=5)
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError('Isolated PostgreSQL restore container did not become ready')
        with dump.open('rb') as source:
            run('docker', 'exec', '-i', name, 'pg_restore', '-U', 'restore_user',
                '-d', 'restore_check', '--exit-on-error', '--single-transaction',
                '--no-owner', '--no-acl', stdin=source, stdout=subprocess.DEVNULL)
        counts = {}
        for table in ('telegram_account', 'group_account_membership', 'ad_delivery_log', 'system_setting'):
            output = subprocess.check_output(['docker', 'exec', name, 'psql', '-U', 'restore_user',
                '-d', 'restore_check', '-Atc', 'SELECT count(*) FROM ' + table], text=True, timeout=30)
            counts[table] = int(output.strip())
        return {'ok': True, 'counts': counts, 'isolated_container': name}
    finally:
        # Only this newly created drill container and its anonymous volumes are removed.
        run('docker', 'rm', '-f', '-v', name, stdout=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--restore-drill', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    BACKUPS.mkdir(mode=0o700, exist_ok=True)
    target = BACKUPS / time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    target.mkdir(mode=0o700)
    dump = target / 'database.dump'
    with dump.open('wb') as output:
        run('docker', 'exec', PG, 'pg_dump', '-p', PORT, '-U', USER, '-d', DATABASE,
            '-Fc', '--no-owner', '--no-acl', stdout=output)
    with dump.open('rb') as source:
        run('docker', 'exec', '-i', PG, 'pg_restore', '--list', stdin=source, stdout=subprocess.DEVNULL)
    with tarfile.open(target / 'private-config.tar.gz', 'w:gz') as tar:
        for name in ('.env.production', 'docker-compose.production.yml',
                     'docker-compose.oracle-shared-postgres.yml', 'sessions'):
            path = ROOT / name
            if path.exists():
                tar.add(path, arcname=name)
        for name, path in {
            'shared-postgres/vanguard-compose.json': Path('/opt/shared-postgres/stacks/vanguard/compose.json'),
            'shared-postgres/gateway.compose.json': Path('/opt/shared-postgres/gateway.compose.json'),
            'shared-postgres/postgres-gateway.conf': Path('/opt/shared-postgres/postgres-gateway.conf'),
        }.items():
            if path.exists():
                tar.add(path, arcname=name)
    run('docker', 'exec', 'vanguard-redis', 'sh', '-c',
        'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli --rdb /data/vanguard-backup.rdb',
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    run('docker', 'cp', 'vanguard-redis:/data/vanguard-backup.rdb', str(target / 'redis.rdb'), stdout=subprocess.DEVNULL)
    os.chmod(target / 'redis.rdb', 0o600)
    result = {'completed_at': time.time(), 'path': str(target), 'postgres_container': PG,
              'database': DATABASE, 'files': {}}
    for path in target.iterdir():
        result['files'][path.name] = {'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    if args.restore_drill or time.gmtime().tm_wday == 6:
        result['restore_drill'] = restore_drill(dump)
    (target / 'manifest.json').write_text(json.dumps(result))
    temporary = BACKUPS / 'latest.tmp'
    temporary.write_text(json.dumps(result))
    temporary.replace(BACKUPS / 'latest.json')
    complete = sorted(p for p in BACKUPS.iterdir() if p.is_dir() and (p / 'manifest.json').is_file())
    for old in complete[:-7]:
        assert old.resolve().parent == BACKUPS.resolve() and not old.is_symlink()
        shutil.rmtree(old)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
