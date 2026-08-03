from __future__ import annotations

from contextlib import suppress

from codex_deploy_automation import connect, run


COMMANDS = [
    "hostname; whoami; date",
    "ss -ltnp | grep ':6379' || true",
    "systemctl is-active redis-server redis || true",
    "docker ps -a --format '{{.Names}} {{.Image}} {{.Status}} {{.Networks}} {{.Ports}}' | grep -Ei 'redis|xboard|vanguard' || true",
    "docker network ls | grep -E 'vanguard|Vanguard' || true",
    "docker network ls | grep -Ei 'xboard|redis|default' || true",
    "docker inspect xboard-redis-1 --format '{{json .NetworkSettings.Networks}}' || true",
    "docker inspect xboard-redis-1 --format 'Cmd={{json .Config.Cmd}}' || true",
    "docker exec xboard-redis-1 sh -lc \"python - <<'PY'\nimport os\nprint('REDIS_PASSWORD_SET=' + ('yes' if os.getenv('REDIS_PASSWORD') else 'no'))\nPY\" || true",
    "docker exec xboard-redis-1 sh -lc \"redis-cli ping || true; redis-cli -a \\\"$REDIS_PASSWORD\\\" ping || true\" || true",
    "docker inspect xboard-redis-1 --format '{{range .NetworkSettings.Networks}}{{.NetworkID}} {{.IPAddress}} {{end}}' || true",
    "docker inspect xboard-redis-1 --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' || true",
    "docker inspect vanguard-backend --format '{{range .NetworkSettings.Networks}}{{.Gateway}} {{.IPAddress}} {{end}}' || true",
    "docker inspect vanguard-backend --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' || true",
    "docker exec vanguard-backend sh -lc \"getent hosts host.docker.internal || true; ip route | head -5 || true\"",
    "docker exec vanguard-backend sh -lc \"python - <<'PY'\nfrom app.core.config import settings\nfor name in ('REDIS_URL', 'CELERY_BROKER_URL', 'CELERY_RESULT_BACKEND'):\n    value = getattr(settings, name)\n    print(f'{name}={value}')\nPY\"",
    "docker exec vanguard-backend sh -lc \"python - <<'PY'\nimport socket\nfor host in ['127.0.0.1', '172.17.0.1', '172.18.0.1', '172.19.0.1', 'host.docker.internal', '137.175.65.47']:\n    try:\n        with socket.create_connection((host, 6379), timeout=2):\n            print(host, 'tcp_ok')\n    except Exception as exc:\n        print(host, type(exc).__name__, str(exc))\nPY\"",
    "docker exec vanguard-backend sh -lc \"python - <<'PY'\nimport socket\nfor host in ['xboard-redis-1', 'redis', 'xboard_redis']:\n    try:\n        print(host, socket.gethostbyname(host))\n    except Exception as exc:\n        print(host, type(exc).__name__, str(exc))\nPY\"",
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
