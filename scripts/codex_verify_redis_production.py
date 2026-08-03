from __future__ import annotations

from contextlib import suppress

from codex_deploy_automation import connect, run


COMMANDS = [
    "curl -fsS http://127.0.0.1:8000/health",
    "docker ps --format '{{.Names}} {{.Status}} {{.Networks}}' | grep vanguard",
    (
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
    ),
    (
        "set -e; "
        "echo '--- celery worker timeout lines since fix ---'; "
        "docker logs --since 10m vanguard-celery-worker 2>&1 | grep -Ei 'Cannot connect|Timeout connecting' || true; "
        "echo '--- celery worker ready lines since fix ---'; "
        "docker logs --since 10m vanguard-celery-worker 2>&1 | grep -Ei 'Connected to redis|ready' || true; "
        "echo '--- celery beat timeout lines since fix ---'; "
        "docker logs --since 10m vanguard-celery-beat 2>&1 | grep -Ei 'Cannot connect|Timeout connecting' || true"
    ),
]


def main() -> int:
    client = connect()
    try:
        for command in COMMANDS:
            run(client, command, timeout=180, allow_fail=True)
    finally:
        with suppress(Exception):
            client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
