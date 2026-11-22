"""Package incremental omission fixes without unrelated working-tree edits."""
import difflib
import hashlib
import io
import json
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[2]
STAGE = ROOT / 'tmp/send-result-omissions'


def main():
    changed = []
    with tarfile.open(STAGE / 'overlay.tar.gz', 'w:gz') as archive:
        for before in sorted((STAGE / 'baseline/backend').rglob('*.py')):
            relative = before.relative_to(STAGE / 'baseline/backend')
            after = ROOT / 'backend' / relative
            old, new = before.read_text(encoding='utf-8'), after.read_text(encoding='utf-8')
            if old == new:
                continue
            patch = ''.join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                fromfile='a/' + relative.as_posix(), tofile='b/' + relative.as_posix())).encode()
            info = tarfile.TarInfo('patches/' + relative.as_posix().replace('/', '__') + '.patch')
            info.size = len(patch)
            archive.addfile(info, io.BytesIO(patch))
            changed.append(relative.as_posix())
        for path in (ROOT / 'backend/tests').rglob('*.py'):
            archive.add(path, arcname=path.relative_to(ROOT / 'backend').as_posix())
        archive.add(ROOT / 'backend/pyproject.toml', arcname='pyproject.toml')
        archive.add(ROOT / 'scripts/ops/send_result_omissions_release.py', arcname='operator_release.py')
        for name in ('alerts.yml', 'send-result-rules.test.yml'):
            archive.add(ROOT / 'monitoring' / name, arcname='monitoring/' + name)
    manifest = {'files': changed, 'sha256': hashlib.sha256((STAGE / 'overlay.tar.gz').read_bytes()).hexdigest()}
    (STAGE / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest))


if __name__ == '__main__':
    main()
