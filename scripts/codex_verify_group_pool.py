from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import paramiko
import socks


SSH_HOST = "137.175.65.47"
SSH_PORT = 58243
SSH_USER = "root"
SSH_KEY = Path(r"D:\tanxuan\proxy-app\sshkey\id_rsa")
PROXY_HOST = "127.0.0.1"
PROXY_PORT = 7897
TEST_GROUP_ID = -1009876543210123


def connect() -> paramiko.SSHClient:
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


def run(client: paramiko.SSHClient, command: str, timeout: int = 180) -> str:
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


def main() -> int:
    client = connect()
    try:
        run(client, "curl -fsS http://127.0.0.1:8000/health")
        account_info = run(
            client,
            "docker exec -i vanguard-backend python - <<'PY'\n"
            "import asyncio, json\n"
            "from sqlalchemy import select\n"
            "import app.core.database as dbmod\n"
            "from app.core.account.models import TelegramAccount\n"
            "from app.core.group.models import Group, GroupAccountMembership\n"
            f"TEST_GROUP_ID = {TEST_GROUP_ID}\n"
            "\n"
            "async def main():\n"
            "    await dbmod.init_db()\n"
            "    async with dbmod.async_session_factory() as session:\n"
            "        result = await session.execute(select(TelegramAccount.id).order_by(TelegramAccount.id).limit(1))\n"
            "        account_id = result.scalar_one_or_none()\n"
            "        await session.execute(GroupAccountMembership.__table__.delete().where(GroupAccountMembership.telegram_group_id == TEST_GROUP_ID))\n"
            "        await session.execute(Group.__table__.delete().where(Group.group_id == TEST_GROUP_ID))\n"
            "        await session.commit()\n"
            "        print(json.dumps({'account_id': account_id, 'test_group_id': TEST_GROUP_ID}))\n"
            "    await dbmod.close_db()\n"
            "\n"
            "asyncio.run(main())\n"
            "PY",
        ).strip()
        parsed = json.loads(account_info.splitlines()[-1])
        account_id = parsed.get("account_id")

        create_payload = {
            "group_id": TEST_GROUP_ID,
            "title": "Codex临时验证群",
            "username": "codex_verify_group",
            "member_count": 12,
            "status": "active",
            "discovery_source": "keyword_search",
            "source_keyword": "自动加群验证词",
            "join_method": "keyword_auto_join",
            "level": "unrated",
        }
        if account_id is not None:
            create_payload["account_id"] = account_id

        payload_json = json.dumps(create_payload, ensure_ascii=False)
        create_response = run(
            client,
            "curl -fsS -X POST http://127.0.0.1:8000/api/groups "
            "-H 'Content-Type: application/json' "
            f"--data-binary {json.dumps(payload_json)}",
        )
        created = json.loads(create_response)
        group_id = created["id"]
        print(f"created_group_db_id={group_id}", flush=True)

        if account_id is not None:
            dup_payload = json.dumps(
                {
                    "account_id": account_id,
                    "join_method": "keyword_auto_join",
                    "source_keyword": "自动加群验证词",
                },
                ensure_ascii=False,
            )
            duplicate_code = run(
                client,
                "curl -sS -o /tmp/codex_group_dup.json -w '%{http_code}' "
                f"-X POST http://127.0.0.1:8000/api/groups/{group_id}/memberships "
                "-H 'Content-Type: application/json' "
                f"--data-binary {json.dumps(dup_payload)}",
            ).strip()
            duplicate_body = run(client, "cat /tmp/codex_group_dup.json").strip()
            if duplicate_code != "400":
                raise RuntimeError(f"duplicate check expected 400, got {duplicate_code}: {duplicate_body}")
            print(f"duplicate_membership_rejected={duplicate_code} {duplicate_body}", flush=True)
        else:
            print("duplicate_membership_skipped=no_account", flush=True)

        list_response = run(
            client,
            "curl -fsS 'http://127.0.0.1:8000/api/groups?page=1&page_size=5&keyword=Codex'",
        )
        listed = json.loads(list_response)
        if not listed.get("data"):
            raise RuntimeError("created group was not returned by keyword list")
        print("list_after_create_ok=true", flush=True)

        run(client, f"curl -fsS -X DELETE http://127.0.0.1:8000/api/groups/{group_id}")
        print("cleanup_deleted=true", flush=True)
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (socket.error, paramiko.SSHException, RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(f"VERIFY_FAILED: {exc}", file=sys.stderr)
        raise
