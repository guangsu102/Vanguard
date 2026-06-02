from __future__ import annotations

import os
import shlex
import socket
import sys
import time
from pathlib import Path

import paramiko
import socks


ROOT = Path(__file__).resolve().parents[1]
REMOTE_ROOT = "/root/Vanguard"
SSH_HOST = "137.175.65.47"
SSH_PORT = 58243
SSH_USER = "root"
SSH_KEY = Path(r"D:\tanxuan\proxy-app\sshkey\id_rsa")
PROXY_HOST = "127.0.0.1"
PROXY_PORT = 7897

FILES = [
    "backend/app/core/group/models.py",
    "backend/app/core/group/__init__.py",
    "backend/app/core/group/manager.py",
    "backend/app/core/database.py",
    "backend/app/api/groups.py",
    "backend/app/modules/acquisition/search/searcher.py",
    "backend/migrations/012_group_pool_memberships.sql",
    "frontend/src/components/FormDrawer.vue",
    "frontend/src/api/groups.ts",
    "frontend/src/views/Groups.vue",
    "frontend/src/components/StatusTag.vue",
]


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
    return client


def run(client: paramiko.SSHClient, command: str, timeout: int = 900) -> str:
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
    if code != 0:
        raise RuntimeError(f"Command failed ({code}): {command}")
    return out


def ensure_remote_dir(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    parts = remote_dir.strip("/").split("/")
    path = ""
    for part in parts:
        path += f"/{part}"
        try:
            sftp.stat(path)
        except FileNotFoundError:
            sftp.mkdir(path)


def upload_files(client: paramiko.SSHClient) -> None:
    sftp = client.open_sftp()
    try:
        for rel in FILES:
            local = ROOT / rel
            remote = f"{REMOTE_ROOT}/{rel.replace(os.sep, '/')}"
            if not local.exists():
                raise FileNotFoundError(local)
            ensure_remote_dir(sftp, str(Path(remote).parent).replace("\\", "/"))
            sftp.put(str(local), remote)
            print(f"uploaded {rel}", flush=True)
    finally:
        sftp.close()


def main() -> int:
    started = time.time()
    client = connect()
    try:
        run(client, f"cd {shlex.quote(REMOTE_ROOT)} && pwd && docker ps --format '{{{{.Names}}}} {{{{.Status}}}}' | grep vanguard")
        upload_files(client)

        run(
            client,
            "docker exec -i vanguard-backend python - <<'PY'\n"
            "from pathlib import Path\n"
            "code = compile(Path('/app/app/api/groups.py').read_text(), '/app/app/api/groups.py', 'exec')\n"
            "print('backend groups.py compile ok')\n"
            "PY",
            timeout=120,
        )

        run(
            client,
            f"cd {shlex.quote(REMOTE_ROOT)} && "
            "docker cp backend/migrations/012_group_pool_memberships.sql "
            "vanguard-backend:/tmp/012_group_pool_memberships.sql",
            timeout=120,
        )

        run(
            client,
            f"cd {shlex.quote(REMOTE_ROOT)} && "
            "docker exec -i vanguard-backend python - <<'PY'\n"
            "import asyncio\n"
            "from pathlib import Path\n"
            "from sqlalchemy import text\n"
            "import app.core.database as dbmod\n"
            "\n"
            "async def main():\n"
            "    await dbmod.init_db()\n"
            "    sql = Path('/tmp/012_group_pool_memberships.sql').read_text()\n"
            "    statements = [s.strip() for s in sql.split(';') if s.strip()]\n"
            "    async with dbmod.async_session_factory() as session:\n"
            "        for stmt in statements:\n"
            "            await session.execute(text(stmt))\n"
            "        await session.commit()\n"
            "    await dbmod.close_db()\n"
            "    print('migration 012 applied')\n"
            "\n"
            "asyncio.run(main())\n"
            "PY",
            timeout=180,
        )

        run(
            client,
            f"cd {shlex.quote(REMOTE_ROOT)} && docker compose -f docker-compose.production.yml up -d --build backend frontend",
            timeout=1200,
        )

        run(client, "docker ps --format '{{.Names}} {{.Status}}' | grep vanguard", timeout=120)
        run(client, "curl -fsS http://127.0.0.1:8000/health", timeout=120)
        run(
            client,
            "curl -fsS 'http://127.0.0.1:8000/api/groups?page=1&page_size=5' | python -m json.tool | head -80",
            timeout=120,
        )
        print(f"\ndeploy completed in {time.time() - started:.1f}s", flush=True)
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (socket.error, paramiko.SSHException, RuntimeError, OSError) as exc:
        print(f"DEPLOY_FAILED: {exc}", file=sys.stderr)
        raise
