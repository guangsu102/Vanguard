from __future__ import annotations

import argparse
import os
import shlex
import socket
import sys
import tarfile
import time
from contextlib import suppress
from pathlib import Path

import paramiko
import socks

# Remote Docker/build logs can contain UTF-8 symbols (for example BuildKit's
# check marks).  The Windows desktop shell may still expose a GBK stdout
# codec; replace only unrepresentable log characters so deployment verification
# cannot abort after a successful remote command.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

ROOT = Path(__file__).resolve().parents[1]
REMOTE_ROOT = "/opt/vanguard"
REMOTE_BACKUP_ROOT = "/opt/vanguard.file-backups"
REMOTE_ARCHIVE_ROOT = "/tmp"
SSH_HOST = "168.110.23.229"
SSH_PORT = 22
SSH_USER = "root"
SSH_KEY_CANDIDATES = [
    Path("E:/sshkey/sshkey/id_rsa"),
]
PROXY_HOST = "127.0.0.1"
PROXY_PORT = 7897
HEALTH_URL = "http://127.0.0.1:18080/health"

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
    ".venv",
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
    ".patch",
}

APP_CONTAINERS = [
    "vanguard-backend",
    "vanguard-frontend",
    "vanguard-celery-worker",
    "vanguard-celery-beat",
    "vanguard-resource-search-worker",
    "vanguard-telegram-growth-worker",
    "vanguard-telegram-guardian-worker",
    "vanguard-bot",
]

MAINLINE_SERVICES = [
    "backend",
    "frontend",
    "celery-worker",
    "resource-search-worker",
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
    ssh_key = next((path for path in SSH_KEY_CANDIDATES if path.exists()), None)
    if ssh_key is None:
        joined = ", ".join(str(path) for path in SSH_KEY_CANDIDATES)
        raise FileNotFoundError(f"SSH key not found in: {joined}")

    sock = socks.socksocket()
    sock.set_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT)
    sock.settimeout(30)
    sock.connect((SSH_HOST, SSH_PORT))

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    key = paramiko.RSAKey.from_private_key_file(str(ssh_key))
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


def assert_oracle4c24g_target() -> None:
    if SSH_HOST != "168.110.23.229" or SSH_PORT != 22:
        raise RuntimeError("Vanguard deployment must target ssh oracle4c24g only")


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
            f"curl -fsS {HEALTH_URL} && exit 0; "
            "sleep 2; "
            "done; "
            f"curl -v --max-time 10 {HEALTH_URL}"
        ),
        timeout=180,
    )


def upload_archive(client: paramiko.SSHClient, archive_path: Path, timestamp: str) -> tuple[paramiko.SSHClient, str]:
    remote_archive = f"{REMOTE_ARCHIVE_ROOT}/.codex-vanguard-full-deploy-{timestamp}.tar.gz"
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


def ensure_remote_dir(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    parts = remote_dir.strip("/").split("/")
    path = ""
    for part in parts:
        path += f"/{part}"
        try:
            sftp.stat(path)
        except FileNotFoundError:
            sftp.mkdir(path)


def upload_single_file(client: paramiko.SSHClient, rel_path: str) -> str:
    local_path = ROOT / rel_path
    if not local_path.exists():
        raise FileNotFoundError(local_path)

    remote_path = f"{REMOTE_ROOT}/{rel_path.replace(os.sep, '/')}"
    with client.open_sftp() as sftp:
        ensure_remote_dir(sftp, str(Path(remote_path).parent).replace("\\", "/"))
        sftp.put(str(local_path), remote_path)
    print(f"uploaded {rel_path}", flush=True)
    return remote_path


def remote_prepare_command(remote_archive: str, timestamp: str) -> str:
    containers = " ".join(shlex.quote(item) for item in APP_CONTAINERS)
    remote_root = shlex.quote(REMOTE_ROOT)
    backup_parent = shlex.quote(REMOTE_BACKUP_ROOT)
    backup_root = shlex.quote(f"{REMOTE_BACKUP_ROOT}/Vanguard.{timestamp}")
    archive = shlex.quote(remote_archive)
    return (
        "set -e; "
        f"mkdir -p {backup_parent}; "
        # Stop only the Vanguard Compose project before moving its bind-mount
        # directory. This leaves the independent Sub2API containers untouched.
        f"if [ -f {remote_root}/docker-compose.production.yml ]; then "
        f"cd {remote_root} && docker compose --env-file .env.production -f docker-compose.production.yml down --remove-orphans || true; fi; "
        f"docker rm -f {containers} >/dev/null 2>&1 || true; "
        f"if [ -d {remote_root} ]; then mv {remote_root} {backup_root}; fi; "
        f"mkdir -p {remote_root}; "
        f"tar -xzf {archive} -C {remote_root}; "
        f"if [ -f {backup_root}/.env.production ]; then cp {backup_root}/.env.production {remote_root}/.env.production; fi; "
        f"if [ -d {backup_root}/data ]; then mv {backup_root}/data {remote_root}/data; fi; "
        f"if [ -d {backup_root}/sessions ]; then mv {backup_root}/sessions {remote_root}/sessions; fi; "
        # Keep database bind-mount ownership intact across releases; only the
        # application-owned log/upload/session directories are writable by
        # UID 1000.
        f"mkdir -p {remote_root}/data/logs {remote_root}/data/uploads {remote_root}/sessions; "
        f"chown -R 1000:1000 {remote_root}/data/logs {remote_root}/data/uploads {remote_root}/sessions || true; "
        f"cd {remote_root}; "
        "docker compose --env-file .env.production -f docker-compose.production.yml config --services"
    )



def apply_migrations_compose_command() -> str:
    """Run the curated migration chain before long-lived app containers.

    A one-off backend container keeps schema changes ahead of application
    startup and uses the runner's default migration list.
    """

    return (
        f"cd {shlex.quote(REMOTE_ROOT)} && "
        "docker compose --env-file .env.production -f docker-compose.production.yml run --rm backend "
        "env PYTHONPATH=/app python /app/scripts/apply_sql_migrations.py"
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
        "}\n"
        "defaults = {'', 'replace-with-shared-secret', 'replace-with-callback-secret'}\n"
        "for key, value in checks.items():\n"
        "    print(f'{key}=' + ('OK' if value and value not in defaults else 'MISSING_OR_DEFAULT'))\n"
        "PY"
    )


KEYWORD_TUNING_SCRIPT = r'''
import asyncio

from sqlalchemy import select

from app.core.account.models import AccountOperationConfig
from app.core.ai.keyword_generator import validate_search_keyword_text
from app.core.database import close_db, get_db_session, init_db
from app.modules.acquisition.models import (
    GroupSearchKeyword,
    SearchKeywordSource,
    SearchKeywordStatus,
)

AUTO_SOURCES = {SearchKeywordSource.AI, SearchKeywordSource.AUTOMATION}


async def main() -> None:
    stats = {
        "configs_updated": 0,
        "keywords_seen": 0,
        "discarded_invalid": 0,
        "approved_pending_auto": 0,
        "review_flags_cleared": 0,
    }
    invalid_examples: list[str] = []
    approved_examples: list[str] = []

    await init_db(create_tables=False)
    try:
        async with get_db_session() as db:
            config_rows = await db.execute(select(AccountOperationConfig))
            for config in config_rows.scalars().all():
                changed = False
                if not config.keyword_auto_replenish_enabled:
                    config.keyword_auto_replenish_enabled = True
                    changed = True
                if config.keyword_replenish_requires_review:
                    config.keyword_replenish_requires_review = False
                    changed = True
                if changed:
                    stats["configs_updated"] += 1

            keyword_rows = await db.execute(select(GroupSearchKeyword))
            for keyword in keyword_rows.scalars().all():
                stats["keywords_seen"] += 1
                ok, reason = validate_search_keyword_text(keyword.text or "")
                if not ok:
                    if keyword.status != SearchKeywordStatus.DISCARDED or keyword.enabled:
                        keyword.status = SearchKeywordStatus.DISCARDED
                        keyword.enabled = False
                        keyword.requires_review = False
                        stats["discarded_invalid"] += 1
                        if len(invalid_examples) < 20:
                            invalid_examples.append(f"{keyword.text}:{reason}")
                    continue

                if keyword.source in AUTO_SOURCES and keyword.status == SearchKeywordStatus.PENDING:
                    keyword.status = SearchKeywordStatus.APPROVED
                    keyword.requires_review = False
                    keyword.enabled = True
                    stats["approved_pending_auto"] += 1
                    if len(approved_examples) < 20:
                        approved_examples.append(keyword.text)
                elif keyword.source in AUTO_SOURCES and keyword.requires_review:
                    keyword.requires_review = False
                    stats["review_flags_cleared"] += 1
    finally:
        await close_db()

    print("keyword_tuning_stats=", stats)
    print("invalid_examples=", invalid_examples)
    print("approved_examples=", approved_examples)


asyncio.run(main())
'''


def run_keyword_tuning(client: paramiko.SSHClient) -> None:
    remote_host_script = "/tmp/codex_keyword_tune.py"
    remote_container_script = "/tmp/codex_keyword_tune.py"
    with client.open_sftp() as sftp:
        with sftp.file(remote_host_script, "w") as handle:
            handle.write(KEYWORD_TUNING_SCRIPT)
    try:
        run(
            client,
            (
                f"docker cp {shlex.quote(remote_host_script)} "
                f"vanguard-backend:{shlex.quote(remote_container_script)} && "
                f"docker exec -i -w /app vanguard-backend "
                f"env PYTHONPATH=/app python {shlex.quote(remote_container_script)}"
            ),
            timeout=300,
        )
    finally:
        run(client, f"rm -f {shlex.quote(remote_host_script)}", timeout=120, allow_fail=True)
        run(
            client,
            f"docker exec -u 0 -i vanguard-backend rm -f {shlex.quote(remote_container_script)}",
            timeout=120,
            allow_fail=True,
        )


GROUP_JOIN_STATE_REPAIR_SCRIPT = r'''
import asyncio
import json
from collections import Counter
from datetime import datetime

from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import selectinload

from app.core.database import close_db, get_db_session, init_db
from app.core.group.models import Group, GroupAccountMembership
from app.modules.acquisition.models import AutoJoinAttempt

TARGET_GROUP_IDS = {3844987433, 2079723581, 1883541915}
TARGET_USERNAMES = {"dnyqxqj", "furryaicanvas", "tiktokhawk"}
PENDING_TOKENS = (
    "join_request_pending",
    "request to join",
    "requested to join",
    "successfully requested",
    "invite_request_sent",
    "inviterequestsent",
    "approval",
    "pending",
    "verification_manual_required",
    "verification_low_confidence",
    "captcha_manual_required",
)
REJECT_TOKENS = (
    "cannot_send_messages",
    "non_chinese_chat",
    "insufficient_chinese_evidence",
    "verification_failed",
    "verification_unknown",
    "verification_leave_required",
    "target is a channel",
    "only groups are allowed",
    "public_username_required",
)


def enum_value(value):
    return value.value if hasattr(value, "value") else value


def dt(value):
    return value.isoformat() if value else None


def attempt_blob(attempt):
    return " ".join(
        str(item or "")
        for item in (
            attempt.status,
            attempt.reason,
            attempt.error,
        )
    ).lower()


def is_pending_attempt(attempt):
    text = attempt_blob(attempt)
    return attempt.status == "pending" or any(token in text for token in PENDING_TOKENS)


def is_rejected_attempt(attempt):
    text = attempt_blob(attempt)
    if "dry_run" in text:
        return False
    return attempt.status in {"failed", "skipped"} and any(token in text for token in REJECT_TOKENS)


def membership_payload(membership):
    account = getattr(membership, "account", None)
    return {
        "id": membership.id,
        "account_id": membership.account_id,
        "account": getattr(account, "identifier", None) or getattr(account, "phone", None),
        "status": membership.status,
        "joined_at": dt(membership.joined_at),
        "left_at": dt(membership.left_at),
        "note": (membership.note or "")[:500],
    }


def attempt_payload(attempt):
    return {
        "id": attempt.id,
        "account_id": attempt.account_id,
        "status": attempt.status,
        "reason": attempt.reason,
        "error": (attempt.error or "")[:500],
        "attempted_at": dt(attempt.attempted_at),
        "joined_at": dt(attempt.joined_at),
    }


async def load_groups(db):
    rows = await db.execute(
        select(Group)
        .options(selectinload(Group.account_memberships).selectinload(GroupAccountMembership.account))
        .where(
            Group.discovery_source == "auto_keyword_search",
            or_(
                Group.group_id.in_(TARGET_GROUP_IDS),
                func.lower(Group.username).in_(TARGET_USERNAMES),
            ),
        )
        .order_by(Group.id)
    )
    return list(rows.scalars().all())


async def latest_attempts(db, group):
    rows = await db.execute(
        select(AutoJoinAttempt)
        .where(
            or_(
                AutoJoinAttempt.group_id == group.id,
                AutoJoinAttempt.telegram_group_id == group.group_id,
            )
        )
        .order_by(desc(AutoJoinAttempt.attempted_at), desc(AutoJoinAttempt.id))
        .limit(5)
    )
    return list(rows.scalars().all())


async def auto_status_counts(db):
    rows = await db.execute(
        select(Group.status, Group.level, Group.discovery_source, func.count(Group.id))
        .where(Group.discovery_source == "auto_keyword_search")
        .group_by(Group.status, Group.level, Group.discovery_source)
        .order_by(Group.status, Group.level)
    )
    return [
        {
            "status": status,
            "level": enum_value(level),
            "source": source,
            "count": count,
        }
        for status, level, source, count in rows.all()
    ]


async def summarize(db):
    payload = []
    for group in await load_groups(db):
        attempts = await latest_attempts(db, group)
        payload.append(
            {
                "id": group.id,
                "telegram_group_id": group.group_id,
                "title": group.title,
                "username": group.username,
                "status": group.status,
                "level": enum_value(group.level),
                "source_keyword": group.source_keyword,
                "member_count": group.member_count,
                "memberships": [membership_payload(item) for item in group.account_memberships],
                "attempts": [attempt_payload(item) for item in attempts],
            }
        )
    return payload


async def repair(db):
    now = datetime.utcnow()
    changes = []
    for group in await load_groups(db):
        attempts = await latest_attempts(db, group)
        latest_pending_attempt = next((item for item in attempts if is_pending_attempt(item)), None)
        statuses = [membership.status for membership in group.account_memberships]
        joined = "joined" in statuses
        pending = "pending" in statuses
        rejected_or_left = any(status in {"left", "rejected"} for status in statuses)

        for membership in group.account_memberships:
            if membership.status == "pending" and membership.left_at is not None:
                membership.left_at = None
                membership.updated_at = now
                changes.append(
                    {
                        "group": group.username or group.group_id,
                        "action": "clear_pending_left_at",
                        "membership_id": membership.id,
                    }
                )

        if latest_pending_attempt and not any(
            membership.account_id == latest_pending_attempt.account_id
            and membership.status == "pending"
            for membership in group.account_memberships
        ):
            db.add(
                GroupAccountMembership(
                    group_id=group.id,
                    telegram_group_id=group.group_id,
                    account_id=latest_pending_attempt.account_id,
                    status="pending",
                    join_method="auto_keyword_search",
                    source_keyword=latest_pending_attempt.source_keyword or group.source_keyword,
                    joined_at=latest_pending_attempt.attempted_at or now,
                    left_at=None,
                    last_checked_at=now,
                    note=json.dumps(
                        {
                            "reason": latest_pending_attempt.reason or "join_request_pending",
                            "error": latest_pending_attempt.error,
                            "repaired_from_attempt_id": latest_pending_attempt.id,
                        },
                        ensure_ascii=False,
                    )[:4000],
                )
            )
            pending = True
            changes.append(
                {
                    "group": group.username or group.group_id,
                    "action": "create_pending_membership",
                    "attempt_id": latest_pending_attempt.id,
                }
            )

        new_status = None
        if not joined:
            if pending or latest_pending_attempt:
                new_status = "pending"
            elif rejected_or_left or (attempts and is_rejected_attempt(attempts[0])):
                new_status = "rejected"

        if new_status and group.status != new_status:
            old_status = group.status
            group.status = new_status
            group.updated_at = now
            changes.append(
                {
                    "group": group.username or group.group_id,
                    "action": "update_group_status",
                    "from": old_status,
                    "to": new_status,
                }
            )

    await db.commit()
    return changes


async def main():
    await init_db(create_tables=False)
    try:
        async with get_db_session() as db:
            before = await summarize(db)
            before_counts = await auto_status_counts(db)
            changes = await repair(db)
            after = await summarize(db)
            after_counts = await auto_status_counts(db)
            print(
                json.dumps(
                    {
                        "before_counts": before_counts,
                        "before": before,
                        "changes": changes,
                        "after_counts": after_counts,
                        "after": after,
                        "change_count": len(changes),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
    finally:
        await close_db()


asyncio.run(main())
'''


def run_group_join_state_repair(client: paramiko.SSHClient) -> None:
    remote_host_script = "/tmp/codex_repair_group_join_state.py"
    remote_container_script = "/tmp/codex_repair_group_join_state.py"
    with client.open_sftp() as sftp:
        with sftp.file(remote_host_script, "w") as handle:
            handle.write(GROUP_JOIN_STATE_REPAIR_SCRIPT)
    try:
        run(
            client,
            (
                f"docker cp {shlex.quote(remote_host_script)} "
                f"vanguard-backend:{shlex.quote(remote_container_script)} && "
                f"docker exec -i -w /app vanguard-backend "
                f"env PYTHONPATH=/app python {shlex.quote(remote_container_script)}"
            ),
            timeout=300,
        )
    finally:
        run(client, f"rm -f {shlex.quote(remote_host_script)}", timeout=120, allow_fail=True)
        run(
            client,
            f"docker exec -u 0 -i vanguard-backend rm -f {shlex.quote(remote_container_script)}",
            timeout=120,
            allow_fail=True,
        )


PENDING_JOIN_SYNC_SCRIPT = r'''
import asyncio
import json

from sqlalchemy import desc, or_, select
from sqlalchemy.orm import selectinload

from app.core.database import close_db, get_db_session, init_db
from app.core.group.models import Group, GroupAccountMembership
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.models import AutoJoinAttempt

TARGET_GROUP_ID = 1883541915


def dt(value):
    return value.isoformat() if value else None


def enum_value(value):
    return value.value if hasattr(value, "value") else value


async def summarize(db):
    row = await db.execute(
        select(Group)
        .options(selectinload(Group.account_memberships).selectinload(GroupAccountMembership.account))
        .where(Group.group_id == TARGET_GROUP_ID)
    )
    group = row.scalar_one_or_none()
    if group is None:
        return None
    attempts = await db.execute(
        select(AutoJoinAttempt)
        .where(or_(AutoJoinAttempt.group_id == group.id, AutoJoinAttempt.telegram_group_id == group.group_id))
        .order_by(desc(AutoJoinAttempt.attempted_at), desc(AutoJoinAttempt.id))
        .limit(5)
    )
    return {
        "id": group.id,
        "telegram_group_id": group.group_id,
        "title": group.title,
        "username": group.username,
        "status": group.status,
        "level": enum_value(group.level),
        "memberships": [
            {
                "id": membership.id,
                "account_id": membership.account_id,
                "account": getattr(membership.account, "identifier", None),
                "status": membership.status,
                "joined_at": dt(membership.joined_at),
                "left_at": dt(membership.left_at),
                "last_checked_at": dt(membership.last_checked_at),
                "note": (membership.note or "")[:1000],
            }
            for membership in group.account_memberships
        ],
        "attempts": [
            {
                "id": attempt.id,
                "status": attempt.status,
                "reason": attempt.reason,
                "error": (attempt.error or "")[:500],
                "attempted_at": dt(attempt.attempted_at),
            }
            for attempt in attempts.scalars().all()
        ],
    }


async def main():
    await init_db(create_tables=False)
    try:
        async with get_db_session() as db:
            service = AcquisitionAutomationService(db)
            configs = await service._list_join_enabled_account_configs(20)
            await service._sync_account_pool([config.account for config in configs])
            before = await summarize(db)
            result = await service._sync_pending_auto_join_memberships()
            after = await summarize(db)
            print(json.dumps({"before": before, "sync_result": result, "after": after}, ensure_ascii=False, indent=2))
    finally:
        await close_db()


asyncio.run(main())
'''


def run_pending_join_sync(client: paramiko.SSHClient) -> None:
    remote_host_script = "/tmp/codex_sync_pending_joins.py"
    remote_container_script = "/tmp/codex_sync_pending_joins.py"
    with client.open_sftp() as sftp:
        with sftp.file(remote_host_script, "w") as handle:
            handle.write(PENDING_JOIN_SYNC_SCRIPT)
    try:
        run(
            client,
            (
                f"docker cp {shlex.quote(remote_host_script)} "
                f"vanguard-backend:{shlex.quote(remote_container_script)} && "
                f"docker exec -i -w /app vanguard-backend "
                f"env PYTHONPATH=/app python {shlex.quote(remote_container_script)}"
            ),
            timeout=300,
        )
    finally:
        run(client, f"rm -f {shlex.quote(remote_host_script)}", timeout=120, allow_fail=True)
        run(
            client,
            f"docker exec -u 0 -i vanguard-backend rm -f {shlex.quote(remote_container_script)}",
            timeout=120,
            allow_fail=True,
        )


KEYWORD_PRIVATE_REPLY_PAUSE_SCRIPT = r'''
from app.core.runtime_settings import load_runtime_settings, save_runtime_settings


def main() -> None:
    raw = load_runtime_settings()
    keyword_private_reply = raw.get("keywordPrivateReply", {})
    if not isinstance(keyword_private_reply, dict):
        keyword_private_reply = {}
    keyword_private_reply["enabled"] = False
    raw["keywordPrivateReply"] = keyword_private_reply

    private_messaging = raw.get("privateMessaging", {})
    if not isinstance(private_messaging, dict):
        private_messaging = {}
    private_messaging.pop("enabled", None)
    private_messaging["inboundRepliesEnabled"] = True
    private_messaging["proactiveEnabled"] = False
    raw["privateMessaging"] = private_messaging

    save_runtime_settings(raw)
    print("keywordPrivateReply.enabled=false privateMessaging.inboundRepliesEnabled=true privateMessaging.proactiveEnabled=false")


main()
'''


def pause_keyword_private_reply(client: paramiko.SSHClient) -> None:
    remote_host_script = "/tmp/codex_pause_keyword_private_reply.py"
    remote_container_script = "/tmp/codex_pause_keyword_private_reply.py"
    with client.open_sftp() as sftp:
        with sftp.file(remote_host_script, "w") as handle:
            handle.write(KEYWORD_PRIVATE_REPLY_PAUSE_SCRIPT)
    try:
        run(
            client,
            (
                f"docker cp {shlex.quote(remote_host_script)} "
                f"vanguard-backend:{shlex.quote(remote_container_script)} && "
                f"docker exec -i -w /app vanguard-backend "
                f"env PYTHONPATH=/app python {shlex.quote(remote_container_script)}"
            ),
            timeout=300,
        )
    finally:
        run(client, f"rm -f {shlex.quote(remote_host_script)}", timeout=120, allow_fail=True)
        run(
            client,
            f"docker exec -u 0 -i vanguard-backend rm -f {shlex.quote(remote_container_script)}",
            timeout=120,
            allow_fail=True,
        )


def main() -> int:
    assert_oracle4c24g_target()
    parser = argparse.ArgumentParser(description="Deploy Vanguard to oracle4c24g.")
    parser.add_argument("--check", action="store_true", help="Only inspect the remote deployment state.")
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="Initialize the production schema and administrator on a fresh target (explicit opt-in).",
    )
    parser.add_argument(
        "--tune-search-keywords",
        action="store_true",
        help="Clean existing low-quality group-search keywords and enable auto-approved replenishment.",
    )
    parser.add_argument(
        "--repair-group-join-state",
        action="store_true",
        help="Repair stale auto-keyword group statuses after join verification/audit outcomes.",
    )
    parser.add_argument(
        "--sync-pending-joins",
        action="store_true",
        help="Run the production pending auto-join state synchronizer once.",
    )
    parser.add_argument(
        "--deploy-scheduler-tasks",
        action="store_true",
        help="Deploy only backend/app/core/scheduler/tasks.py and restart Celery services.",
    )
    parser.add_argument(
        "--deploy-acquisition-automation",
        action="store_true",
        help="Deploy acquisition automation/keyword files and restart auto-join services.",
    )
    parser.add_argument(
        "--pause-keyword-private-reply",
        action="store_true",
        help="Deploy and disable keyword-triggered private replies in production.",
    )
    parser.add_argument(
        "--deploy-concurrency-runtime",
        action="store_true",
        help="Deploy account-concurrency runtime files and restart backend/worker services.",
    )
    parser.add_argument(
        "--deploy-ad-policy-controls",
        action="store_true",
        help="Deploy ad policy switch wiring, retired-budget cleanup, and UI labels.",
    )
    args = parser.parse_args()

    if args.check:
        client = connect()
        try:
            run(client, "docker ps -a --format '{{.Names}} {{.Status}}' | grep vanguard || true", timeout=120)
            run(client, "docker logs --tail 200 vanguard-backend", timeout=180, allow_fail=True)
            run(client, "docker logs --tail 120 vanguard-celery-worker", timeout=180, allow_fail=True)
            run(client, "docker logs --tail 120 vanguard-telegram-growth-worker", timeout=180, allow_fail=True)
            run(client, "docker logs --tail 120 vanguard-telegram-guardian-worker", timeout=180, allow_fail=True)
            run(client, f"curl -v --max-time 10 {HEALTH_URL}", timeout=120, allow_fail=True)
            run(client, env_check_command(), timeout=180, allow_fail=True)
            run(client, f"cd {shlex.quote(REMOTE_ROOT)} && docker compose --env-file .env.production -f docker-compose.production.yml ps", timeout=120, allow_fail=True)
            run(client, f"rm -f {shlex.quote(REMOTE_ARCHIVE_ROOT)}/.codex-vanguard-full-deploy-*.tar.gz", timeout=120, allow_fail=True)
            return 0
        finally:
            with suppress(Exception):
                client.close()

    if args.tune_search_keywords:
        client = connect()
        try:
            run(client, "docker ps --format '{{.Names}} {{.Status}}' | grep vanguard-backend", timeout=120)
            run_keyword_tuning(client)
            return 0
        finally:
            with suppress(Exception):
                client.close()

    if args.repair_group_join_state:
        client = connect()
        try:
            run(client, "docker ps --format '{{.Names}} {{.Status}}' | grep vanguard-backend", timeout=120)
            run_group_join_state_repair(client)
            return 0
        finally:
            with suppress(Exception):
                client.close()

    if args.sync_pending_joins:
        client = connect()
        try:
            run(client, "docker ps --format '{{.Names}} {{.Status}}' | grep vanguard-backend", timeout=120)
            run_pending_join_sync(client)
            return 0
        finally:
            with suppress(Exception):
                client.close()

    if args.pause_keyword_private_reply:
        started = time.time()
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        rel_paths = [
            "backend/app/core/runtime_settings.py",
            "backend/app/modules/acquisition/keyword_trigger/handler.py",
            "backend/app/modules/acquisition/keyword_trigger/actions.py",
            "backend/app/modules/acquisition/private_msg/private_handler.py",
            "backend/app/modules/acquisition/private_msg/guide_flow.py",
            "backend/app/modules/acquisition/tracking/tracker.py",
            "backend/app/api/settings.py",
            "frontend/src/api/settings.ts",
            "frontend/src/views/Settings.vue",
        ]
        backup_dir = f"{REMOTE_BACKUP_ROOT}/keyword-private-reply-pause-{timestamp}"
        build_log = f"/tmp/codex-keyword-private-reply-build-{timestamp}.log"
        services = "backend frontend telegram-growth-worker"
        client = connect()
        try:
            run(client, "docker ps --format '{{.Names}} {{.Status}}' | grep vanguard", timeout=120)
            run(client, f"mkdir -p {shlex.quote(backup_dir)}", timeout=120)
            for rel_path in rel_paths:
                remote_path = f"{REMOTE_ROOT}/{rel_path}"
                backup_path = f"{backup_dir}/{rel_path.replace('/', '__')}"
                run(
                    client,
                    f"if [ -f {shlex.quote(remote_path)} ]; then cp {shlex.quote(remote_path)} {shlex.quote(backup_path)}; fi",
                    timeout=120,
                )
                upload_single_file(client, rel_path)
            run(
                client,
                (
                    f"docker run --rm -v {REMOTE_ROOT}/backend:/code python:3.12-slim "
                    "python -m py_compile "
                    "/code/app/core/runtime_settings.py "
                    "/code/app/modules/acquisition/keyword_trigger/handler.py "
                    "/code/app/modules/acquisition/keyword_trigger/actions.py "
                    "/code/app/modules/acquisition/private_msg/private_handler.py "
                    "/code/app/modules/acquisition/private_msg/guide_flow.py "
                    "/code/app/modules/acquisition/tracking/tracker.py "
                    "/code/app/api/settings.py"
                ),
                timeout=180,
            )
            run(
                client,
                (
                    f"cd {shlex.quote(REMOTE_ROOT)} && "
                    f"(docker compose --env-file .env.production -f docker-compose.production.yml build {services} "
                    f"> {shlex.quote(build_log)} 2>&1; "
                    "code=$?; "
                    f"tail -n 200 {shlex.quote(build_log)}; "
                    "exit $code)"
                ),
                timeout=2400,
            )
            run(
                client,
                (
                    f"cd {shlex.quote(REMOTE_ROOT)} && "
                    f"docker compose --env-file .env.production -f docker-compose.production.yml up -d --force-recreate {services}"
                ),
                timeout=900,
            )
            wait_for_health(client)
            pause_keyword_private_reply(client)
            run(
                client,
                (
                    "docker exec -i -w /app vanguard-backend env PYTHONPATH=/app python -c "
                    + shlex.quote(
                        "from app.core.runtime_settings import get_private_messaging_settings,is_keyword_private_reply_enabled,is_private_messaging_enabled;"
                        "print('keyword_private_reply_enabled=', is_keyword_private_reply_enabled());"
                        "print('private_messaging_settings=', get_private_messaging_settings());"
                        "print('private_inbound_enabled=', is_private_messaging_enabled(initiated_by_user=True));"
                        "print('private_proactive_enabled=', is_private_messaging_enabled(initiated_by_user=False))"
                    )
                ),
                timeout=120,
            )
            run(
                client,
                (
                    "docker exec -i vanguard-backend python - <<'PY'\n"
                    "import asyncio\n"
                    "from sqlalchemy import text\n"
                    "from app.core.database import close_db, get_db_session, init_db\n"
                    "async def main():\n"
                    "    await init_db(create_tables=False)\n"
                    "    try:\n"
                    "        async with get_db_session() as db:\n"
                    "            rows = await db.execute(text(\"select action, enabled, count(*) from acquisition_keyword_trigger group by action, enabled order by action, enabled\"))\n"
                    "            print([tuple(row) for row in rows.all()])\n"
                    "    finally:\n"
                    "        await close_db()\n"
                    "asyncio.run(main())\n"
                    "PY"
                ),
                timeout=180,
                allow_fail=True,
            )
            run(client, "docker ps --format '{{.Names}} {{.Status}}' | grep vanguard", timeout=120)
            run(client, "docker logs --since 3m --tail 120 vanguard-backend", timeout=180, allow_fail=True)
            run(client, "docker logs --since 3m --tail 120 vanguard-telegram-growth-worker", timeout=180, allow_fail=True)
            run(client, f"rm -f {shlex.quote(build_log)}", timeout=120, allow_fail=True)
            print(f"\nkeyword private reply pause completed in {time.time() - started:.1f}s", flush=True)
            return 0
        finally:
            with suppress(Exception):
                client.close()

    if args.deploy_concurrency_runtime:
        started = time.time()
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        rel_paths = [
            "backend/app/modules/acquisition/automation.py",
            "backend/app/core/scheduler/tasks.py",
            "backend/app/core/account/pool.py",
            "backend/app/core/runtime_settings.py",
            "backend/app/api/automation.py",
            "frontend/src/api/automation.ts",
            "frontend/src/views/Automation.vue",
        ]
        backup_dir = f"{REMOTE_BACKUP_ROOT}/concurrency-runtime-{timestamp}"
        services = "backend frontend celery-worker celery-beat telegram-growth-worker telegram-guardian-worker"
        client = connect()
        try:
            run(client, "docker ps --format '{{.Names}} {{.Status}}' | grep vanguard", timeout=120)
            run(client, f"mkdir -p {shlex.quote(backup_dir)}", timeout=120)
            for rel_path in rel_paths:
                remote_path = f"{REMOTE_ROOT}/{rel_path}"
                backup_path = f"{backup_dir}/{rel_path.replace('/', '__')}"
                run(
                    client,
                    f"if [ -f {shlex.quote(remote_path)} ]; then cp {shlex.quote(remote_path)} {shlex.quote(backup_path)}; fi",
                    timeout=120,
                )
                upload_single_file(client, rel_path)
            run(
                client,
                (
                    f"docker run --rm -v {REMOTE_ROOT}/backend:/code python:3.12-slim "
                    "python -m py_compile "
                    "/code/app/modules/acquisition/automation.py "
                    "/code/app/core/scheduler/tasks.py "
                    "/code/app/core/account/pool.py "
                    "/code/app/core/runtime_settings.py "
                    "/code/app/api/automation.py"
                ),
                timeout=180,
            )
            run(
                client,
                (
                    f"cd {shlex.quote(REMOTE_ROOT)} && "
                    f"docker compose --env-file .env.production -f docker-compose.production.yml build {services}"
                ),
                timeout=2400,
            )
            run(
                client,
                (
                    f"cd {shlex.quote(REMOTE_ROOT)} && "
                    f"docker compose --env-file .env.production -f docker-compose.production.yml up -d --force-recreate {services}"
                ),
                timeout=900,
            )
            wait_for_health(client)
            run(client, "docker ps --format '{{.Names}} {{.Status}}' | grep vanguard", timeout=120)
            run(client, "docker logs --since 3m --tail 120 vanguard-backend", timeout=180, allow_fail=True)
            run(client, "docker logs --since 3m --tail 120 vanguard-celery-worker", timeout=180, allow_fail=True)
            run(client, "docker logs --since 3m --tail 80 vanguard-celery-beat", timeout=180, allow_fail=True)
            run(client, "docker logs --since 3m --tail 120 vanguard-telegram-growth-worker", timeout=180, allow_fail=True)
            print(f"\nconcurrency runtime deploy completed in {time.time() - started:.1f}s", flush=True)
            return 0
        finally:
            with suppress(Exception):
                client.close()

    if args.deploy_ad_policy_controls:
        started = time.time()
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        rel_paths = [
            "backend/app/modules/acquisition/automation.py",
            "backend/app/core/automation_settings.py",
            "backend/app/core/runtime_settings.py",
            "backend/app/core/account/risk_guard.py",
            "backend/app/api/automation.py",
            "backend/scripts/apply_sql_migrations.py",
            "backend/migrations/045_remove_ad_delivery_risk_budget.sql",
            "backend/migrations/046_remove_redundant_probe_configuration.sql",
            "frontend/src/views/GrowthDashboard.vue",
            "frontend/src/views/Automation.vue",
            "frontend/src/api/automation.ts",
            "frontend/src/config/automationDefaults.ts",
        ]
        backup_dir = f"{REMOTE_BACKUP_ROOT}/Vanguard.ad-policy-controls-{timestamp}"
        build_log = f"/tmp/codex-ad-policy-controls-build-{timestamp}.log"
        services = (
            "backend frontend celery-worker resource-search-worker celery-beat "
            "telegram-growth-worker telegram-guardian-worker"
        )
        client = connect()
        try:
            settings_sql = (
                "select key, value from system_setting "
                "where key in ('automation.account_risk_guard','automation.ad_capacity') "
                "order by key"
            )
            migration_sql = (
                "select filename from schema_migration_history "
                "where filename = '046_remove_redundant_probe_configuration.sql'"
            )
            settings_psql = (
                "psql -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\" -Atc "
                f"{shlex.quote(settings_sql)}"
            )
            migration_psql = (
                "psql -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\" -Atc "
                f"{shlex.quote(migration_sql)}"
            )
            settings_check = """
import asyncio
from app.core.automation_settings import get_account_risk_guard_settings, get_ad_capacity_settings
from app.core.account.risk_guard import AccountRiskAction, AccountRiskGuard
from app.core.database import close_db, get_db_session, init_db

async def main():
    await init_db(create_tables=False)
    try:
        async with get_db_session() as db:
            risk = await get_account_risk_guard_settings(db)
            capacity = await get_ad_capacity_settings(db)
            probe_budget = AccountRiskGuard._budget_for_action(AccountRiskAction.AD_PROBE, risk)
            print("risk_actions=", sorted(risk.get("actions", {})))
            print("has_ad_delivery_action=", "ad_delivery" in risk.get("actions", {}))
            print("has_editable_ad_probe_action=", "ad_probe" in risk.get("actions", {}))
            print("internal_ad_probe_budget=", {"daily_limit": probe_budget.daily_limit, "cooldown_seconds": probe_budget.cooldown_seconds})
            print("has_retired_global_probe_limit=", "ad_policy_auto_probe_daily_limit" in capacity)
            print("probe_limit_per_account=", capacity.get("ad_policy_auto_probe_daily_limit_per_account"))
            print("switches=", {key: capacity.get(key) for key in (
                "leave_on_deleted_ad", "block_group_on_probe_failure",
                "ad_policy_ai_require_second_pass",
            )})
    finally:
        await close_db()

asyncio.run(main())
"""
            run(
                client,
                "docker ps --format '{{.Names}} {{.Status}}' | grep vanguard",
                timeout=120,
            )
            run(client, f"mkdir -p {shlex.quote(backup_dir)}", timeout=120)
            run(
                client,
                "docker ps --format '{{.Names}} {{.ID}} {{.Status}}' | "
                f"grep -E '^(vanguard|sub2api-dr)' > {shlex.quote(backup_dir + '/containers.before')}",
                timeout=120,
                allow_fail=True,
            )
            run(
                client,
                "docker inspect --format '{{.Id}} {{.State.Status}}' "
                "sub2api-dr-app-candidate-622ee4881 sub2api-dr-postgres sub2api-dr-redis "
                f"> {shlex.quote(backup_dir + '/sub2api.before')}",
                timeout=120,
                allow_fail=True,
            )
            for rel_path in rel_paths:
                remote_path = f"{REMOTE_ROOT}/{rel_path}"
                backup_path = f"{backup_dir}/{rel_path.replace('/', '__')}"
                run(
                    client,
                    f"if [ -f {shlex.quote(remote_path)} ]; then cp -p "
                    f"{shlex.quote(remote_path)} {shlex.quote(backup_path)}; fi",
                    timeout=120,
                )
                upload_single_file(client, rel_path)

            # Save the pre-migration settings rows without printing their
            # contents. They are the precise rollback artifacts for the
            # retired/duplicate probe configuration.
            run(
                client,
                (
                    f"cd {shlex.quote(REMOTE_ROOT)} && "
                    "docker compose --env-file .env.production -f docker-compose.production.yml exec -T postgres "
                    f"sh -lc {shlex.quote(settings_psql)} "
                    f"> {shlex.quote(backup_dir + '/probe-settings.before')}; "
                    f"sha256sum {shlex.quote(backup_dir + '/probe-settings.before')}"
                ),
                timeout=180,
                allow_fail=True,
            )
            run(
                client,
                (
                    f"docker run --rm -v {REMOTE_ROOT}/backend:/code python:3.12-slim "
                    "python -m py_compile "
                    "/code/app/modules/acquisition/automation.py "
                    "/code/app/core/automation_settings.py "
                    "/code/app/core/runtime_settings.py "
                    "/code/app/core/account/risk_guard.py "
                    "/code/app/api/automation.py "
                    "/code/scripts/apply_sql_migrations.py"
                ),
                timeout=180,
            )
            run(
                client,
                (
                    f"cd {shlex.quote(REMOTE_ROOT)} && "
                    f"(docker compose --env-file .env.production -f docker-compose.production.yml build {services} "
                    f"> {shlex.quote(build_log)} 2>&1; code=$?; "
                    f"tail -n 240 {shlex.quote(build_log)}; exit $code)"
                ),
                timeout=3000,
            )
            run(client, apply_migrations_compose_command(), timeout=1200)
            run(
                client,
                (
                    f"cd {shlex.quote(REMOTE_ROOT)} && "
                    f"docker compose --env-file .env.production -f docker-compose.production.yml up -d --force-recreate {services}"
                ),
                timeout=1200,
            )
            wait_for_health(client)
            run(
                client,
                "docker ps --format '{{.Names}} {{.ID}} {{.Status}}' | "
                f"grep -E '^(vanguard|sub2api-dr)' > {shlex.quote(backup_dir + '/containers.after')}",
                timeout=120,
            )
            run(
                client,
                "docker inspect --format '{{.Id}} {{.State.Status}}' "
                "sub2api-dr-app-candidate-622ee4881 sub2api-dr-postgres sub2api-dr-redis "
                f"> {shlex.quote(backup_dir + '/sub2api.after')}; "
                f"diff -u {shlex.quote(backup_dir + '/sub2api.before')} "
                f"{shlex.quote(backup_dir + '/sub2api.after')} || true",
                timeout=120,
                allow_fail=True,
            )
            run(
                client,
                (
                    f"cd {shlex.quote(REMOTE_ROOT)} && "
                    "docker compose --env-file .env.production -f docker-compose.production.yml exec -T postgres "
                    f"sh -lc {shlex.quote(migration_psql)}"
                ),
                timeout=180,
            )
            run(
                client,
                (
                    "docker exec -i vanguard-backend python -c "
                    + shlex.quote(f"exec({settings_check!r})")
                ),
                timeout=180,
            )
            run(
                client,
                "docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "
                "vanguard-backend | grep -E '^(OWNED_GROUP_EXECUTION_ENABLED|P0_SAFETY_GATE_ENABLED|P0_SAFETY_GATE_FAIL_CLOSED|OWNED_GROUP_KILL_SWITCH_ENABLED)=' | sort",
                timeout=120,
            )
            run(client, "curl -fsS https://vanguard.pipenai.xyz/health", timeout=120)
            run(
                client,
                "grep -nE 'server_name|proxy_pass' /etc/nginx/conf.d/vanguard.conf",
                timeout=120,
                allow_fail=True,
            )
            run(client, "docker logs --since 5m --tail 160 vanguard-backend", timeout=180, allow_fail=True)
            run(client, "docker logs --since 5m --tail 100 vanguard-telegram-growth-worker", timeout=180, allow_fail=True)
            run(client, f"rm -f {shlex.quote(build_log)}", timeout=120, allow_fail=True)
            print(
                f"\nad policy controls deploy completed in {time.time() - started:.1f}s; "
                f"backup={backup_dir}",
                flush=True,
            )
            return 0
        finally:
            with suppress(Exception):
                client.close()

    if args.deploy_acquisition_automation:
        started = time.time()
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        rel_paths = [
            "backend/app/modules/acquisition/automation.py",
            "backend/app/modules/acquisition/config.py",
            "backend/app/modules/acquisition/models.py",
            "backend/app/modules/acquisition/search/filters.py",
            "backend/app/modules/acquisition/search/group_finder.py",
            "backend/app/modules/acquisition/search_keyword_registry.py",
            "backend/app/modules/acquisition/keyword_trigger/matcher.py",
            "backend/app/core/account/models.py",
            "backend/app/core/runtime_settings.py",
            "backend/app/api/acquisition.py",
            "backend/app/api/automation.py",
            "backend/app/api/group_search_keywords.py",
            "backend/app/api/groups.py",
            "backend/app/core/ai/keyword_generator.py",
            "backend/app/core/ai/llm_client.py",
            "backend/scripts/apply_sql_migrations.py",
            "frontend/src/api/acquisition.ts",
            "frontend/src/api/automation.ts",
            "frontend/src/api/groups.ts",
            "frontend/src/components/StatusTag.vue",
            "frontend/src/views/Automation.vue",
            "frontend/src/views/Groups.vue",
            "frontend/src/views/Keywords.vue",
        ]
        backup_dir = f"{REMOTE_BACKUP_ROOT}/acquisition-automation-{timestamp}"
        services = "backend frontend celery-worker celery-beat telegram-growth-worker"
        client = connect()
        try:
            run(client, "docker ps --format '{{.Names}} {{.Status}}' | grep vanguard", timeout=120)
            run(client, f"mkdir -p {shlex.quote(backup_dir)}", timeout=120)
            for rel_path in rel_paths:
                remote_path = f"{REMOTE_ROOT}/{rel_path}"
                backup_path = f"{backup_dir}/{rel_path.replace('/', '__')}"
                run(
                    client,
                    f"if [ -f {shlex.quote(remote_path)} ]; then cp {shlex.quote(remote_path)} {shlex.quote(backup_path)}; fi",
                    timeout=120,
                )
                upload_single_file(client, rel_path)
            run(
                client,
                (
                    f"docker run --rm -v {REMOTE_ROOT}/backend:/code python:3.12-slim "
                    "python -m py_compile "
                    "/code/app/modules/acquisition/automation.py "
                    "/code/app/modules/acquisition/config.py "
                    "/code/app/modules/acquisition/models.py "
                    "/code/app/modules/acquisition/search/filters.py "
                    "/code/app/modules/acquisition/search/group_finder.py "
                    "/code/app/modules/acquisition/search_keyword_registry.py "
                    "/code/app/modules/acquisition/keyword_trigger/matcher.py "
                    "/code/app/core/account/models.py "
                    "/code/app/core/runtime_settings.py "
                    "/code/app/api/acquisition.py "
                    "/code/app/api/automation.py "
                    "/code/app/api/group_search_keywords.py "
                    "/code/app/api/groups.py "
                    "/code/app/core/ai/keyword_generator.py "
                    "/code/app/core/ai/llm_client.py "
                    "/code/scripts/apply_sql_migrations.py"
                ),
                timeout=180,
            )
            run(
                client,
                (
                    f"cd {shlex.quote(REMOTE_ROOT)} && "
                    f"docker compose --env-file .env.production -f docker-compose.production.yml build {services}"
                ),
                timeout=2400,
            )
            run(client, apply_migrations_compose_command(), timeout=900)
            run(
                client,
                (
                    f"cd {shlex.quote(REMOTE_ROOT)} && "
                    f"docker compose --env-file .env.production -f docker-compose.production.yml up -d --force-recreate {services}"
                ),
                timeout=900,
            )
            wait_for_health(client)
            run(client, "docker ps --format '{{.Names}} {{.Status}}' | grep vanguard", timeout=120)
            run(client, "docker logs --since 3m --tail 120 vanguard-backend", timeout=180, allow_fail=True)
            run(client, "docker logs --since 3m --tail 120 vanguard-celery-worker", timeout=180, allow_fail=True)
            run(client, "docker logs --since 3m --tail 80 vanguard-celery-beat", timeout=180, allow_fail=True)
            print(f"\nacquisition automation deploy completed in {time.time() - started:.1f}s", flush=True)
            return 0
        finally:
            with suppress(Exception):
                client.close()

    if args.deploy_scheduler_tasks:
        started = time.time()
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        rel_path = "backend/app/core/scheduler/tasks.py"
        remote_path = f"{REMOTE_ROOT}/{rel_path}"
        backup_dir = f"{REMOTE_BACKUP_ROOT}/scheduler-tasks-{timestamp}"
        client = connect()
        try:
            run(client, "docker ps --format '{{.Names}} {{.Status}}' | grep vanguard", timeout=120)
            run(
                client,
                (
                    f"mkdir -p {shlex.quote(backup_dir)} && "
                    f"cp {shlex.quote(remote_path)} "
                    f"{shlex.quote(backup_dir)}/tasks.py"
                ),
                timeout=120,
            )
            upload_single_file(client, rel_path)
            run(
                client,
                (
                    f"docker run --rm -v {REMOTE_ROOT}/backend:/code python:3.12-slim "
                    "python -m py_compile /code/app/core/scheduler/tasks.py"
                ),
                timeout=180,
            )
            run(
                client,
                (
                    f"cd {shlex.quote(REMOTE_ROOT)} && "
                    "docker compose --env-file .env.production -f docker-compose.production.yml build celery-worker celery-beat"
                ),
                timeout=2400,
            )
            run(
                client,
                (
                    f"cd {shlex.quote(REMOTE_ROOT)} && "
                    "docker compose --env-file .env.production -f docker-compose.production.yml up -d --force-recreate celery-worker celery-beat"
                ),
                timeout=900,
            )
            run(client, "docker ps --format '{{.Names}} {{.Status}}' | grep vanguard", timeout=120)
            run(client, "docker logs --since 2m --tail 120 vanguard-celery-worker", timeout=180, allow_fail=True)
            run(client, "docker logs --since 2m --tail 80 vanguard-celery-beat", timeout=180, allow_fail=True)
            print(f"\nscheduler task deploy completed in {time.time() - started:.1f}s", flush=True)
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
        compose_root = shlex.quote(REMOTE_ROOT)
        build_services = f"{services} db-init admin-init" if args.bootstrap else services
        run(
            client,
            f"cd {compose_root} && docker compose --env-file .env.production -f docker-compose.production.yml build {build_services}",
            timeout=3600,
        )
        run(
            client,
            f"cd {compose_root} && docker compose --env-file .env.production -f docker-compose.production.yml up -d postgres redis",
            timeout=600,
        )
        if args.bootstrap:
            run(
                client,
                f"cd {compose_root} && docker compose --env-file .env.production -f docker-compose.production.yml --profile bootstrap run --rm db-init",
                timeout=1200,
            )
            run(
                client,
                f"cd {compose_root} && docker compose --env-file .env.production -f docker-compose.production.yml --profile bootstrap run --rm --no-deps admin-init",
                timeout=600,
            )
        else:
            run(client, apply_migrations_compose_command(), timeout=1200)
        run(client, f"cd {shlex.quote(REMOTE_ROOT)} && docker compose --env-file .env.production -f docker-compose.production.yml up -d --force-recreate {services}", timeout=1200)

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
