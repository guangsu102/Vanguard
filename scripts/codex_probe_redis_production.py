from __future__ import annotations

from contextlib import suppress

from codex_deploy_automation import connect, run


COMMANDS = [
    "hostname; whoami; date",
    "ss -ltnp | grep ':6379' || true",
    "systemctl is-active redis-server redis || true",
    "docker ps -a --format '{{.Names}} {{.Image}} {{.Status}} {{.Networks}} {{.Ports}}' | grep -Ei 'redis|vanguard' || true",
    "docker network ls | grep -Ei 'vanguard|redis' || true",
    "docker inspect vanguard-redis --format '{{json .NetworkSettings.Networks}}' || true",
    "docker inspect vanguard-redis --format 'Cmd={{json .Config.Cmd}}' || true",
    "docker exec vanguard-redis sh -lc 'if [ -n \"$REDIS_PASSWORD\" ]; then redis-cli --no-auth-warning -a \"$REDIS_PASSWORD\" ping; else redis-cli ping; fi' || true",
    "docker inspect vanguard-redis --format '{{range .NetworkSettings.Networks}}{{.NetworkID}} {{.IPAddress}} {{end}}' || true",
    "docker inspect vanguard-redis --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' || true",
    "docker inspect vanguard-backend --format '{{range .NetworkSettings.Networks}}{{.Gateway}} {{.IPAddress}} {{end}}' || true",
    "docker inspect vanguard-backend --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' || true",
    "docker exec vanguard-backend sh -lc \"getent hosts redis || true; ip route | head -5 || true\"",
    "docker exec -i vanguard-backend python - <<'PY'\nfrom urllib.parse import urlsplit\nimport redis\nfrom app.core.config import settings\nfor name in ('REDIS_URL', 'CELERY_BROKER_URL', 'CELERY_RESULT_BACKEND'):\n    value = getattr(settings, name)\n    parsed = urlsplit(value)\n    print(f'{name}: host={parsed.hostname} db={parsed.path.lstrip(\"/\")} password_set={bool(parsed.password)}')\n    try:\n        client = redis.Redis.from_url(value, socket_connect_timeout=2, socket_timeout=2)\n        print(f'{name}_ping={client.ping()}')\n    except Exception as exc:\n        print(f'{name}_error={type(exc).__name__}')\nPY",
    "docker exec -i vanguard-backend python - <<'PY'\nimport socket\ntry:\n    with socket.create_connection(('redis', 6379), timeout=2):\n        print('redis_tcp_ok')\nexcept Exception as exc:\n    print('redis_tcp_error=' + type(exc).__name__)\nPY",
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
