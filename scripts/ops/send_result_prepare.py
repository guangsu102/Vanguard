"""Build on oracle4c24g from its own production baseline and bounded patches."""
from pathlib import Path
import json
import shutil
import subprocess
import tarfile
from datetime import datetime

ROOT = Path('/opt/vanguard/releases/send-result-20261113-r1')
IMAGE = 'local/vanguard:send-result-20261113-r1'


def extract(path, dest):
    with tarfile.open(path) as archive:
        for member in archive.getmembers():
            if member.name.startswith('/') or '..' in Path(member.name).parts:
                raise ValueError('unsafe_archive_member')
        archive.extractall(dest, filter='data')


def main():
    context = ROOT / 'context'
    if context.exists():
        if (ROOT / 'published.json').exists():
            raise RuntimeError('candidate already published')
        context.rename(ROOT / ('context.failed-' + datetime.now().strftime('%H%M%S')))
    context.mkdir()
    extract(ROOT / 'worker-source.tar.gz', context)
    for source in (context / 'app').rglob('*.py'):
        source.write_text(source.read_text())
    overlay = ROOT / 'overlay'
    overlay.mkdir(exist_ok=True)
    extract(ROOT / 'overlay.tar.gz', overlay)
    errors = []
    for path in sorted((overlay / 'patches').glob('*.patch')):
        if path.name == 'app__modules__account_spam__service.py.patch':
            service = (context / 'app/modules/account_spam/service.py').read_text()
            if 'config.auto_ads_enabled = True' not in service and 'config.auto_join_enabled = True' not in service:
                continue  # The deployed version already preserves operator switches on a clear result.
        if path.name == 'app__core__scheduler__growth_dispatch.py.patch':
            dispatcher = context / 'app/core/scheduler/growth_dispatch.py'
            source = dispatcher.read_text()
            assert '"daily_survival_and_exit": Stage("growth_maintenance", 600, 2, 2)' in source
            source = source.replace('"daily_survival_and_exit": Stage("growth_maintenance", 600, 2, 2)', '"exit": Stage("growth_maintenance", 60, 2, 2)')
            source = source.replace('"survival": "daily_survival_and_exit", "exit": "daily_survival_and_exit"', '"exit": "exit"')
            dispatcher.write_text(source)
            continue
        result = subprocess.run(['patch', '--batch', '--forward', '--fuzz=0', '-p1', '-d', str(context)],
                                input=path.read_bytes(), capture_output=True)
        if result.returncode:
            print(result.stdout.decode(), result.stderr.decode())
            errors.append(path.name)
    if errors:
        raise RuntimeError('patch_failed:' + ','.join(errors))
    shutil.copytree(overlay / 'new', context, dirs_exist_ok=True)
    # Preserve the currently deployed user endpoint fix exactly.
    with tarfile.open(ROOT / 'api-source.tar.gz') as archive:
        (context / 'app/api/users.py').write_bytes(archive.extractfile('app/api/users.py').read())
    main = context / 'app/main.py'
    main.write_text(main.read_text() + '\nfrom app.modules.acquisition.send_result_metrics import install\ninstall(app)\n')
    shutil.copytree(overlay / 'tests', context / 'tests')
    shutil.copy2(overlay / 'pyproject.toml', context / 'pyproject.toml')
    shutil.copytree(overlay / 'dist', context / 'dist')
    compose = json.loads((ROOT / 'compose.baseline.json').read_text())
    base = compose['services']['telegram-growth-worker-survival-continuity']['image']
    (context / 'Dockerfile').write_text(f'FROM {base}\nCOPY --chown=appuser:appgroup app /app/app\nCOPY --chown=appuser:appgroup scripts /app/scripts\nRUN python -m compileall -q /app/app /app/scripts\n')
    (context / 'Dockerfile.frontend').write_text('FROM ' + compose['services']['frontend']['image'] + '\nCOPY dist /usr/share/nginx/html\n')
    (context / 'Dockerfile.test').write_text(f'FROM {IMAGE}\nUSER root\nRUN pip install --disable-pip-version-check "fakeredis[lua]" aiosqlite pytest-asyncio ruff\nCOPY tests /app/tests\nCOPY pyproject.toml /app/pyproject.toml\n')
    for suffix, dockerfile in [('', 'Dockerfile'), ('-frontend', 'Dockerfile.frontend'), ('-test', 'Dockerfile.test')]:
        with (ROOT / ('build' + suffix + '.log')).open('w') as log:
            subprocess.run(['docker', 'build', '-t', IMAGE + suffix, '-f', str(context / dockerfile), str(context)],
                           stdout=log, stderr=subprocess.STDOUT, check=True)
    print(json.dumps({'built': IMAGE, 'preserved_users_fix': True}))
    (ROOT / 'built.json').write_text(json.dumps({'image': IMAGE}))


if __name__ == '__main__':
    main()
