from __future__ import annotations

import os
import shlex
import socket
import sys
import tarfile
import time
import argparse
from contextlib import suppress
from pathlib import Path

import paramiko
import socks


ROOT = Path(__file__).resolve().parents[1]
REMOTE_ROOT = "/root/Vanguard"
SSH_HOST = "137.175.65.47"
SSH_PORT = 58243
SSH_USER = "root"
SSH_KEY = Path(os.environ["TEMP"]) / "codex-ssh-vanguard" / "id_rsa"
PROXY_HOST = "127.0.0.1"
PROXY_PORT = 7897

ARCHIVE_PATH = ROOT / ".codex-vanguard-full-deploy.tar.gz"

INCLUDE_DIRS = [
    "backend",
    "bot-matrix",
    "deploy",
    "docs",
    "frontend",
    "monitoring",
    "nginx",
    "scripts",
]

SKIP_DIR_NAMES = {
    ".chrome-vanguard-verify",
    ".codex-ssh",
    ".git",
    ".pytest_cache",
    "__pycache__",
    "dist",
    "htmlcov",
    "logs",
    "node_modules",
    "sessions",
    "tmp",
    "temp",
}
SKIP_FILE_NAMES = {
    ".coverage",
    ".codex-vanguard-full-deploy.tar.gz",
}
SKIP_SUFFIXES = {
    ".db",
    ".log",
    ".pyc",
    ".pyo",
    ".session",
    ".sqlite",
    ".tar.gz",
    ".zip",
}

APP_CONTAINERS = [
    "vanguard-backend",
    "vanguard-frontend",
    "vanguard-celery-worker",
    "vanguard-celery-beat",
    "vanguard-telegram-growth-worker",
    "vanguard-telegram-guardian-worker",
    "vanguard-bot",
]

MAINLINE_SERVICES = [
    "backend",
    "frontend",
    "celery-worker",
    "celery-beat",
    "telegram-growth-worker",
    "telegram-guardian-worker",
]


def _should_skip(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if path.is_dir() and path.name in SKIP_DIR_NAMES:
        return True
    if any(part in SKIP_DIR_NAMES for part in rel.parts):
        return True
    if path.name in SKIP_FILE_NAMES:
        return True
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in SKIP_SUFFIXES)


def _iter_files() -> list[Path]:
    files: list[Path] = []

    for child in ROOT.iterdir():
        if child.is_file() and not _should_skip(child):
            files.append(child)

    for rel_dir in INCLUDE_DIRS:
        directory = ROOT / rel_dir
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path.is_file() and not _should_skip(path):
                files.append(path)

    return sorted(set(files), key=lambda item: item.relative_to(ROOT).as_posix())


def build_archive() -> tuple[Path, int]:
    with suppress(FileNotFoundError):
        ARCHIVE_PATH.unlink()

    files = _iter_files()
    with tarfile.open(ARCHIVE_PATH, "w:gz") as archive:
        for path in files:
            archive.add(path, arcname=path.relative_to(ROOT).as_posix(), recursive=False)

    return ARCHIVE_PATH, len(files)


def connect() -> paramiko.SSHClient:
    if not SSH_KEY.exists():
        raise FileNotFoundError(f"SSH key not found: {SSH_KEY}")

    sock = socks.socksocket()
    sock.set_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT)
    sock.settimeout(30)
    sock.connect((SSH_HOST, SSH_PORT))

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    key = paramiko.RSAKey.from_private_key_file(str(SSH_KEY))
    client.connect(
        SSH_HOST,
        port=SSH_PORT,
        username=SSH_USER,
        pkey=key,
        sock=sock,
        timeout=30,
        banner_timeout=30,
        auth_timeout=30,
    )
    transport = client.get_transport()
    if transport is not None:
        transport.set_keepalive(30)
    return client


def run(client: paramiko.SSHClient, command: str, timeout: int = 900, allow_fail: bool = False) -> str:
    print(f"\n$ {command}", flush=True)
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    stdin.close()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    if out:
        print(out, end="" if out.endswith("\n") else "\n", flush=True)
    if err:
        print(err, end="" if err.endswith("\n") else "\n", flush=True)
    if code != 0 and not allow_fail:
        raise RuntimeError(f"Command failed ({code}): {command}")
    return out


def wait_for_health(client: paramiko.SSHClient) -> None:
    run(
        client,
        (
            "for i in $(seq 1 30); do "
            "curl -fsS http://127.0.0.1:8000/health && exit 0; "
            "sleep 2; "
            "done; "
            "curl -v --max-time 10 http://127.0.0.1:8000/health"
        ),
        timeout=180,
    )


def upload_archive(client: paramiko.SSHClient, archive_path: Path, timestamp: str) -> tuple[paramiko.SSHClient, str]:
    remote_archive = f"/root/.codex-vanguard-full-deploy-{timestamp}.tar.gz"
    for attempt in range(1, 4):
        try:
            with client.open_sftp() as sftp:
                sftp.put(str(archive_path), remote_archive)
            print(f"uploaded archive to {remote_archive}", flush=True)
            return client, remote_archive
        except (EOFError, OSError, paramiko.SSHException) as exc:
            print(f"upload retry {attempt}/3: {exc}", flush=True)
            if attempt == 3:
                raise
            client.close()
            client = connect()
    return client, remote_archive


def remote_prepare_command(remote_archive: str, timestamp: str) -> str:
    containers = " ".join(shlex.quote(item) for item in APP_CONTAINERS)
    remote_root = shlex.quote(REMOTE_ROOT)
    backup_root = shlex.quote(f"/root/Vanguard.backups/Vanguard.{timestamp}")
    archive = shlex.quote(remote_archive)
    return (
        "set -e; "
        f"mkdir -p /root/Vanguard.backups; "
        f"docker rm -f {containers} >/dev/null 2>&1 || true; "
        f"if [ -d {remote_root} ]; then mv {remote_root} {backup_root}; fi; "
        f"mkdir -p {remote_root}; "
        f"tar -xzf {archive} -C {remote_root}; "
        f"if [ -f {backup_root}/.env.production ]; then cp {backup_root}/.env.production {remote_root}/.env.production; fi; "
        f"if [ -d {backup_root}/data ]; then mv {backup_root}/data {remote_root}/data; fi; "
        f"if [ -d {backup_root}/sessions ]; then mv {backup_root}/sessions {remote_root}/sessions; fi; "
        f"mkdir -p {remote_root}/data/logs {remote_root}/data/uploads {remote_root}/sessions; "
        f"chown -R 1000:1000 {remote_root}/data {remote_root}/sessions || true; "
        f"cd {remote_root}; "
        "docker compose -f docker-compose.production.yml config --services"
    )


def apply_migrations_command() -> str:
    return (
        "docker exec -i vanguard-backend "
        "env PYTHONPATH=/app python /app/scripts/apply_sql_migrations.py "
        "--files "
        "010_add_telegram_account_session_string.sql "
        "011_acquisition_message_type_varchar.sql "
        "012_group_pool_memberships.sql "
        "013_acquisition_automation_ads.sql "
        "014_account_keyword_replenish_policy.sql "
        "015_growth_guardian_refactor.sql "
        "016_xboard_acquisition_tracking_worker.sql"
    )


def env_check_command() -> str:
    return (
        "docker exec -i vanguard-backend python - <<'PY'\n"
        "from app.core.config import settings\n"
        "checks = {\n"
        "    'VANGUARD_APP_ID': getattr(settings, 'VANGUARD_APP_ID', ''),\n"
        "    'VANGUARD_SIGNING_SECRET': getattr(settings, 'VANGUARD_SIGNING_SECRET', ''),\n"
        "    'VANGUARD_CALLBACK_APP_ID': getattr(settings, 'VANGUARD_CALLBACK_APP_ID', ''),\n"
        "    'VANGUARD_CALLBACK_SIGNING_SECRET': getattr(settings, 'VANGUARD_CALLBACK_SIGNING_SECRET', ''),\n"
        "    'XBOARD_API_URL': getattr(settings, 'XBOARD_API_URL', ''),\n"
        "}\n"
        "defaults = {'', 'replace-with-shared-secret', 'replace-with-callback-secret'}\n"
        "for key, value in checks.items():\n"
        "    print(f'{key}=' + ('OK' if value and value not in defaults else 'MISSING_OR_DEFAULT'))\n"
        "PY"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy Vanguard to xd.")
    parser.add_argument("--check", action="store_true", help="Only inspect the remote deployment state.")
    args = parser.parse_args()

    if args.check:
        client = connect()
        try:
            run(client, "docker ps -a --format '{{.Names}} {{.Status}}' | grep vanguard || true", timeout=120)
            run(client, "docker logs --tail 200 vanguard-backend", timeout=180, allow_fail=True)
            run(client, "docker logs --tail 120 vanguard-celery-worker", timeout=180, allow_fail=True)
            run(client, "docker logs --tail 120 vanguard-telegram-growth-worker", timeout=180, allow_fail=True)
            run(client, "docker logs --tail 120 vanguard-telegram-guardian-worker", timeout=180, allow_fail=True)
            run(client, "curl -v --max-time 10 http://127.0.0.1:8000/health", timeout=120, allow_fail=True)
            run(client, env_check_command(), timeout=180, allow_fail=True)
            run(client, "cd /root/Vanguard && docker compose -f docker-compose.production.yml ps", timeout=120, allow_fail=True)
            run(client, "rm -f /root/.codex-vanguard-full-deploy-*.tar.gz", timeout=120, allow_fail=True)
            return 0
        finally:
            with suppress(Exception):
                client.close()

    started = time.time()
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    archive_path, file_count = build_archive()
    print(f"built full archive: {archive_path} ({file_count} files, {archive_path.stat().st_size} bytes)", flush=True)

    client = connect()
    remote_archive = ""
    try:
        run(client, "pwd; whoami; docker ps --format '{{.Names}} {{.Status}}' | grep vanguard || true", timeout=120)
        client, remote_archive = upload_archive(client, archive_path, timestamp)

        run(client, remote_prepare_command(remote_archive, timestamp), timeout=900)
        services = " ".join(MAINLINE_SERVICES)
        run(client, f"cd {shlex.quote(REMOTE_ROOT)} && docker compose -f docker-compose.production.yml build {services}", timeout=3600)
        run(client, f"cd {shlex.quote(REMOTE_ROOT)} && docker compose -f docker-compose.production.yml up -d --force-recreate backend", timeout=1200)
        run(client, apply_migrations_command(), timeout=600)
        run(client, f"cd {shlex.quote(REMOTE_ROOT)} && docker compose -f docker-compose.production.yml up -d --force-recreate {services}", timeout=1200)

        wait_for_health(client)
        run(client, env_check_command(), timeout=180, allow_fail=True)
        run(client, "docker ps --format '{{.Names}} {{.Status}}' | grep vanguard", timeout=120)
        run(client, "docker logs --tail 100 vanguard-backend", timeout=180, allow_fail=True)
        run(client, "docker logs --tail 80 vanguard-celery-worker", timeout=180, allow_fail=True)
        run(client, "docker logs --tail 80 vanguard-telegram-growth-worker", timeout=180, allow_fail=True)
        run(client, "docker logs --tail 80 vanguard-telegram-guardian-worker", timeout=180, allow_fail=True)
        run(client, f"rm -f {shlex.quote(remote_archive)}", timeout=120, allow_fail=True)

        print(f"\ndeploy completed in {time.time() - started:.1f}s", flush=True)
        return 0
    finally:
        with suppress(Exception):
            client.close()
        with suppress(FileNotFoundError):
            archive_path.unlink()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (socket.error, paramiko.SSHException, RuntimeError, OSError) as exc:
        print(f"DEPLOY_FAILED: {exc}", file=sys.stderr)
        raise
