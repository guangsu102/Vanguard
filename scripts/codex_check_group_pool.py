from __future__ import annotations

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


def run(client: paramiko.SSHClient, command: str, timeout: int = 180) -> int:
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
    print(f"[exit {code}]", flush=True)
    return code


def main() -> int:
    client = connect()
    try:
        commands = [
            "docker ps --format '{{.Names}} {{.Status}}' | grep vanguard",
            "curl -sS http://127.0.0.1:8000/health",
            "docker logs --tail=200 vanguard-backend",
            "docker logs --tail=100 vanguard-frontend",
            "curl -sS 'http://127.0.0.1:8000/api/groups?page=1&page_size=5'",
        ]
        exit_code = 0
        for command in commands:
            code = run(client, command)
            if code != 0:
                exit_code = code
        return exit_code
    finally:
        client.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (socket.error, paramiko.SSHException, OSError) as exc:
        print(f"CHECK_FAILED: {exc}", file=sys.stderr)
        raise
