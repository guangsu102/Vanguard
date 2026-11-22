"""Run the candidate against a disposable PG18, never the business database."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import time

ROOT = Path('/opt/vanguard/releases/send-result-20261113-r1')
IMAGE = 'local/vanguard:send-result-20261113-r1-test'
NETWORK = 'vanguard-send-result-test'
DATABASE = 'vanguard-send-result-test-pg'


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def main():
    password = secrets.token_urlsafe(24)
    run('docker', 'network', 'create', NETWORK, stdout=subprocess.DEVNULL)
    try:
        run('docker', 'run', '-d', '--rm', '--name', DATABASE, '--network', NETWORK,
            '--memory', '512m', '--label', 'vanguard.send-result-test=true',
            '-e', 'POSTGRES_PASSWORD=' + password, '-e', 'POSTGRES_DB=vanguard_growth_test',
            'postgres:18-alpine', stdout=subprocess.DEVNULL)
        for _ in range(30):
            ready = subprocess.run(['docker', 'exec', DATABASE, 'pg_isready', '-U', 'postgres'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if ready.returncode == 0:
                break
            time.sleep(1)
        url = f'postgresql+asyncpg://postgres:{password}@{DATABASE}:5432/vanguard_growth_test'
        env = ROOT / '.test.env'
        fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as out:
            out.write('VANGUARD_TEST_POSTGRES_URL=' + url + '\nDATABASE_URL=' + url + '\nDEBUG=true\n')
        args = ['tests/unit/test_send_result_policy.py', 'tests/unit/test_simplified_growth.py',
                'tests/unit/test_growth_remediation.py', 'tests/unit/test_growth_event_inbox.py',
                'tests/unit/test_growth_dispatch_boundaries.py', 'tests/unit/test_production_diagnosis_regressions.py',
                'tests/integration/test_simplified_growth_postgres.py', '--override-ini', 'addopts=',
                '-q', '--disable-warnings', '--maxfail=5']
        command = 'from tests.conftest import _import_models;_import_models();import pytest,sys;sys.exit(pytest.main(' + repr(args) + '))'
        with (ROOT / 'candidate-tests.log').open('w') as log:
            result = subprocess.run(['docker', 'run', '--rm', '--network', NETWORK, '--memory', '1024m',
                '--env-file', str(env), '--entrypoint', 'python', IMAGE, '-c', command], stdout=log, stderr=subprocess.STDOUT)
        env.unlink()
        print(json.dumps({'tests_exit': result.returncode}))
        print('\n'.join((ROOT / 'candidate-tests.log').read_text().splitlines()[-60:]))
        if result.returncode:
            raise SystemExit(result.returncode)
        (ROOT / 'tests-passed.json').write_text(json.dumps({'passed': True, 'image': IMAGE, 'image_id': json.loads(subprocess.check_output(['docker','image','inspect', IMAGE.removesuffix('-test')]))[0]['Id']}))
    finally:
        subprocess.run(['docker', 'rm', '-f', DATABASE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['docker', 'network', 'rm', NETWORK], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
