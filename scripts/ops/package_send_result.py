"""Package only this change against the saved working-tree baseline."""
import difflib
import hashlib
import io
import json
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[2]
STAGE = ROOT / 'tmp/send-result-20261113'


def main():
    changed = []
    with tarfile.open(STAGE / 'overlay.tar.gz', 'w:gz') as archive:
        for before in sorted((STAGE / 'baseline/backend').rglob('*.py')):
            relative = before.relative_to(STAGE / 'baseline/backend')
            if relative.parts[0] != 'app' or relative.as_posix() == 'app/main.py':
                continue
            after = ROOT / 'backend' / relative
            if before.read_bytes() == after.read_bytes():
                continue
            diff = ''.join(difflib.unified_diff(before.read_text(encoding='utf-8').splitlines(True),
                after.read_text(encoding='utf-8').splitlines(True),
                fromfile='a/' + relative.as_posix(), tofile='b/' + relative.as_posix()))
            data = diff.encode()
            info = tarfile.TarInfo('patches/' + relative.as_posix().replace('/', '__') + '.patch')
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
            changed.append(relative.as_posix())
        for path in (ROOT / 'backend/app/modules/acquisition').glob('send_result*.py'):
            archive.add(path, arcname='new/' + path.relative_to(ROOT / 'backend').as_posix())
        archive.add(ROOT / 'backend/scripts/activate_send_result.py', arcname='new/scripts/activate_send_result.py')
        for path in (ROOT / 'backend/tests').rglob('*.py'):
            archive.add(path, arcname=path.relative_to(ROOT / 'backend').as_posix())
        archive.add(ROOT / 'backend/pyproject.toml', arcname='pyproject.toml')
        for path in (ROOT / 'frontend/dist').rglob('*'):
            if path.is_file():
                archive.add(path, arcname=path.relative_to(ROOT / 'frontend').as_posix())
    digest = hashlib.sha256((STAGE / 'overlay.tar.gz').read_bytes()).hexdigest()
    (STAGE / 'overlay-manifest.json').write_text(json.dumps({'patches': changed, 'sha256': digest}, indent=2))
    print(json.dumps({'patches': len(changed), 'sha256': digest}))


if __name__ == '__main__':
    main()
