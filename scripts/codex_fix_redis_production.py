from __future__ import annotations

import shlex
import time
from contextlib import suppress
from pathlib import Path

from codex_deploy_automation import REMOTE_ROOT, connect, run


ROOT = Path(__file__).resolve().parents[1]
LOCAL_COMPOSE = ROOT / "docker-compose.production.yml"

REDIS_SETTINGS = {
    "REDIS_PASSWORD": "",
    "REDIS_URL": "redis://redis:6379/1",
    "CELERY_BROKER_URL": "redis://redis:6379/1",
    "CELERY_RESULT_BACKEND": "redis://redis:6379/2",
}

SERVICES = [
    "backend",
    "celery-worker",
    "celery-beat",
    "telegram-growth-worker",
    "telegram-guardian-worker",
]


def update_env_command() -> str:
    assignments = "\n".join(f"    {key!r}: {value!r}," for key, value in REDIS_SETTINGS.items())
    return (
        f"cd {shlex.quote(REMOTE_ROOT)} && python3 - <<'PY'\n"
        "from pathlib import Path\n"
        "path = Path('.env.production')\n"
        "updates = {\n"
        f"{assignments}\n"
        "}\n"
        "lines = path.read_text(encoding='utf-8').splitlines()\n"
        "seen = set()\n"
        "output = []\n"
        "for line in lines:\n"
        "    stripped = line.lstrip()\n"
        "    if '=' in line and not stripped.startswith('#'):\n"
        "        key = line.split('=', 1)[0].strip()\n"
        "        if key in updates:\n"
        "            output.append(f'{key}={updates[key]}')\n"
        "            seen.add(key)\n"
        "            continue\n"
        "    output.append(line)\n"
        "for key, value in updates.items():\n"
        "    if key not in seen:\n"
        "        output.append(f'{key}={value}')\n"
        "path.write_text('\\n'.join(output) + '\\n', encoding='utf-8')\n"
        "print('updated redis settings:', ', '.join(updates))\n"
        "PY"
    )


def verify_redis_command() -> str:
    return (
        "docker exec -i vanguard-backend python - <<'PY'\n"
        "from urllib.parse import urlparse\n"
        "import redis\n"
        "from app.core.config import settings\n"
        "for name in ('REDIS_URL', 'CELERY_BROKER_URL', 'CELERY_RESULT_BACKEND'):\n"
        "    parsed = urlparse(getattr(settings, name))\n"
        "    print(f'{name}: host={parsed.hostname} db={parsed.path.lstrip(\"/\")}')\n"
        "for db in (1, 2):\n"
        "    client = redis.Redis.from_url(f'redis://redis:6379/{db}', socket_connect_timeout=2, socket_timeout=2)\n"
        "    print(f'redis_db_{db}_ping=' + str(client.ping()))\n"
        "PY"
    )


def check_logs_command() -> str:
    return (
        "set -e; "
        "echo '--- celery worker recent redis/connect lines ---'; "
        "docker logs --since 90s vanguard-celery-worker 2>&1 | "
        "grep -Ei 'Cannot connect|Timeout connecting|Connected to redis|ready|mingle|error' || true; "
        "echo '--- celery beat recent redis/connect lines ---'; "
        "docker logs --since 90s vanguard-celery-beat 2>&1 | "
        "grep -Ei 'Cannot connect|Timeout connecting|Connected to redis|ready|mingle|error' || true"
    )


def main() -> int:
    if not LOCAL_COMPOSE.exists():
        raise FileNotFoundError(LOCAL_COMPOSE)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = f"/root/Vanguard.file-backups/{timestamp}_redis_fix"
    remote_tmp = f"/root/docker-compose.production.yml.redis-fix.{timestamp}.tmp"
    services = " ".join(SERVICES)

    client = connect()
    try:
        run(client, "docker ps -a --format '{{.Names}} {{.Status}} {{.Networks}}' | grep -E 'vanguard|xboard-redis' || true", timeout=120)
        run(
            client,
            (
                f"set -e; mkdir -p {shlex.quote(backup_dir)}; "
                f"cp -a {shlex.quote(REMOTE_ROOT)}/docker-compose.production.yml {shlex.quote(backup_dir)}/docker-compose.production.yml; "
                f"cp -a {shlex.quote(REMOTE_ROOT)}/.env.production {shlex.quote(backup_dir)}/.env.production; "
                f"echo backup_dir={shlex.quote(backup_dir)}"
            ),
            timeout=120,
        )

        with client.open_sftp() as sftp:
            sftp.put(str(LOCAL_COMPOSE), remote_tmp)
        print(f"uploaded compose to {remote_tmp}", flush=True)

        run(
            client,
            (
                f"set -e; mv {shlex.quote(remote_tmp)} {shlex.quote(REMOTE_ROOT)}/docker-compose.production.yml; "
                f"cd {shlex.quote(REMOTE_ROOT)}; "
                "docker compose -f docker-compose.production.yml config --services"
            ),
            timeout=180,
        )
        run(client, update_env_command(), timeout=120)
        run(client, f"cd {shlex.quote(REMOTE_ROOT)} && docker compose -f docker-compose.production.yml config --quiet", timeout=180)
        run(
            client,
            f"cd {shlex.quote(REMOTE_ROOT)} && docker compose -f docker-compose.production.yml up -d --force-recreate {services}",
            timeout=1200,
        )
        run(
            client,
            "for i in $(seq 1 30); do curl -fsS http://127.0.0.1:8000/health && exit 0; sleep 2; done; curl -v --max-time 10 http://127.0.0.1:8000/health",
            timeout=180,
        )
        run(client, verify_redis_command(), timeout=180)
        run(client, "sleep 8; docker ps --format '{{.Names}} {{.Status}} {{.Networks}}' | grep vanguard", timeout=120)
        run(client, check_logs_command(), timeout=180, allow_fail=True)
        print(f"redis fix completed; backup_dir={backup_dir}", flush=True)
        return 0
    finally:
        with suppress(Exception):
            run(client, f"rm -f {shlex.quote(remote_tmp)}", timeout=60, allow_fail=True)
        with suppress(Exception):
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
