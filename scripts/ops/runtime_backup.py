"""Run on oracle4c24g: daily private backups and isolated PostgreSQL restore drill."""
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


def run(*args, **kwargs):
    return subprocess.run(args, check=True, timeout=300, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--restore-drill', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    BACKUPS.mkdir(mode=0o700, exist_ok=True)
    target = BACKUPS / time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    target.mkdir(mode=0o700)
    with (target / 'database.dump').open('wb') as output:
        run('docker', 'exec', 'vanguard-postgres', 'pg_dump', '-U', 'vanguard', '-d', 'vanguard', '-Fc', stdout=output)
    with (target / 'database.dump').open('rb') as source:
        run('docker', 'exec', '-i', 'vanguard-postgres', 'pg_restore', '--list', stdin=source, stdout=subprocess.DEVNULL)
    with tarfile.open(target / 'private-config.tar.gz', 'w:gz') as tar:
        for name in ('.env.production', 'docker-compose.production.yml', 'sessions'):
            path = ROOT / name
            if path.exists():
                tar.add(path, arcname=name)
    # Snapshot only this dedicated Redis; auth is expanded inside the container.
    run('docker', 'exec', 'vanguard-redis', 'sh', '-c',
        'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli --rdb /data/vanguard-backup.rdb',
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    run('docker', 'cp', 'vanguard-redis:/data/vanguard-backup.rdb', str(target / 'redis.rdb'), stdout=subprocess.DEVNULL)
    os.chmod(target / 'redis.rdb', 0o600)
    result = {'completed_at': time.time(), 'path': str(target), 'files': {}}
    for path in target.iterdir():
        result['files'][path.name] = {'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    if args.restore_drill or time.gmtime().tm_wday == 6:
        database = 'vanguard_restore_' + time.strftime('%Y%m%d_%H%M%S', time.gmtime())
        assert database.startswith('vanguard_restore_') and database != 'vanguard'
        run('docker', 'exec', 'vanguard-postgres', 'createdb', '-U', 'vanguard', database)
        try:
            with (target / 'database.dump').open('rb') as source:
                run('docker', 'exec', '-i', 'vanguard-postgres', 'pg_restore', '-U', 'vanguard',
                    '-d', database, '--exit-on-error', '--no-owner', stdin=source, stdout=subprocess.DEVNULL)
            counts = {}
            for table in ('telegram_account', 'group_account_membership', 'ad_delivery_log', 'system_setting'):
                output = subprocess.check_output(['docker', 'exec', 'vanguard-postgres', 'psql', '-U', 'vanguard',
                    '-d', database, '-Atc', 'SELECT count(*) FROM ' + table], text=True)
                counts[table] = int(output.strip())
            result['restore_drill'] = {'ok': True, 'counts': counts, 'database': database}
        finally:
            run('docker', 'exec', 'vanguard-postgres', 'dropdb', '-U', 'vanguard', database)
    (target / 'manifest.json').write_text(json.dumps(result))
    temporary = BACKUPS / 'latest.tmp'
    temporary.write_text(json.dumps(result))
    temporary.replace(BACKUPS / 'latest.json')
    # Retention only inside this task-owned directory and only complete backups.
    complete = sorted([p for p in BACKUPS.iterdir() if p.is_dir() and (p / 'manifest.json').is_file()])
    for old in complete[:-7]:
        assert old.resolve().parent == BACKUPS.resolve() and not old.is_symlink()
        shutil.rmtree(old)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
