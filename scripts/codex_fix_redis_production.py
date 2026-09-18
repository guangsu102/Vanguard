from __future__ import annotations

import shlex
import time
from contextlib import suppress
from pathlib import Path

from codex_deploy_automation import (
    HEALTH_URL,
    REMOTE_ARCHIVE_ROOT,
    REMOTE_BACKUP_ROOT,
    REMOTE_ROOT,
    connect,
    run,
)

ROOT = Path(__file__).resolve().parents[1]
LOCAL_COMPOSE = ROOT / "docker-compose.production.yml"

REDIS_DATABASES = {
    "REDIS_URL": 1,
    "CELERY_BROKER_URL": 1,
    "CELERY_RESULT_BACKEND": 2,
}

SERVICES = [
    "backend",
    "celery-worker",
    "celery-beat",
    "resource-search-worker",
    "telegram-growth-worker",
    "telegram-guardian-worker",
]


def update_env_command() -> str:
    return (
        f"cd {shlex.quote(REMOTE_ROOT)} && python3 - <<'PY'\n"
        "from pathlib import Path\n"
        "from urllib.parse import quote\n"
        "path = Path('.env.production')\n"
        "lines = path.read_text(encoding='utf-8').splitlines()\n"
        "values = {}\n"
        "for line in lines:\n"
        "    stripped = line.strip()\n"
        "    if not stripped or stripped.startswith('#') or '=' not in line:\n"
        "        continue\n"
        "    key, value = line.split('=', 1)\n"
        "    values[key.strip()] = value.strip().strip(chr(34)).strip(chr(39))\n"
        "password = values.get('REDIS_PASSWORD', '').strip()\n"
        "if not password:\n"
        "    raise SystemExit('REDIS_PASSWORD must be configured in .env.production')\n"
        f"databases = {REDIS_DATABASES!r}\n"
        "encoded_password = quote(password, safe='')\n"
        "updates = {name: f'redis://:{encoded_password}@redis:6379/{db}' for name, db in databases.items()}\n"
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
        "    value = getattr(settings, name)\n"
        "    parsed = urlparse(value)\n"
        "    print(f'{name}: host={parsed.hostname} db={parsed.path.lstrip(\"/\")} password_set={bool(parsed.password)}')\n"
        "    client = redis.Redis.from_url(value, socket_connect_timeout=2, socket_timeout=2)\n"
        "    print(f'{name}_ping=' + str(client.ping()))\n"
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
    backup_dir = f"{REMOTE_BACKUP_ROOT}/{timestamp}_redis_fix"
    remote_tmp = f"{REMOTE_ARCHIVE_ROOT}/docker-compose.production.yml.redis-fix.{timestamp}.tmp"
    services = " ".join(SERVICES)

    client = connect()
    try:
        run(client, "docker ps -a --format '{{.Names}} {{.Status}} {{.Networks}}' | grep -E 'vanguard|redis' || true", timeout=120)
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
                "docker compose --env-file .env.production -f docker-compose.production.yml config --services"
            ),
            timeout=180,
        )
        run(client, update_env_command(), timeout=120)
        run(client, f"cd {shlex.quote(REMOTE_ROOT)} && docker compose --env-file .env.production -f docker-compose.production.yml config --quiet", timeout=180)
        run(
            client,
            f"cd {shlex.quote(REMOTE_ROOT)} && docker compose --env-file .env.production -f docker-compose.production.yml up -d --force-recreate {services}",
            timeout=1200,
        )
        run(
            client,
            f"for i in $(seq 1 30); do curl -fsS {HEALTH_URL} && exit 0; sleep 2; done; curl -v --max-time 10 {HEALTH_URL}",
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
