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

ROOT = Path(__file__).resolve().parents[1]
REMOTE_ROOT = "/root/Vanguard"
SSH_HOST = "107.149.161.99"
SSH_PORT = 28278
SSH_USER = "root"
SSH_KEY_CANDIDATES = [
    Path(os.environ["TEMP"]) / "codex-ssh-vanguard" / "id_rsa",
    Path("D:/tanxuan/proxy-app/sshkey/id_rsa"),
]
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

EXTERNAL_NETWORKS = [
    "xboard_xboard_internal",
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
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
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


def assert_test001_target() -> None:
    if SSH_HOST != "107.149.161.99" or SSH_PORT != 28278:
        raise RuntimeError("Vanguard deployment must target ssh test001 only")


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


def ensure_external_networks_command() -> str:
    parts = []
    for network in EXTERNAL_NETWORKS:
        quoted = shlex.quote(network)
        parts.append(
            f"docker network inspect {quoted} >/dev/null 2>&1 || docker network create {quoted}"
        )
    return "set -e; " + "; ".join(parts)


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
        "016_xboard_acquisition_tracking_worker.sql "
        "017_campaign_execution.sql "
        "018_group_search_keyword_usage.sql "
        "019_group_search_keyword_normalized.sql "
        "020_keyword_trigger_review.sql"
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
    assert_test001_target()
    parser = argparse.ArgumentParser(description="Deploy Vanguard to xd.")
    parser.add_argument("--check", action="store_true", help="Only inspect the remote deployment state.")
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
        backup_dir = f"/root/Vanguard.file-backups/keyword-private-reply-pause-{timestamp}"
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
                    "docker run --rm -v /root/Vanguard/backend:/code python:3.12-slim "
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
                    f"(docker compose -f docker-compose.production.yml build {services} "
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
                    f"docker compose -f docker-compose.production.yml up -d --force-recreate {services}"
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
        backup_dir = f"/root/Vanguard.file-backups/concurrency-runtime-{timestamp}"
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
                    "docker run --rm -v /root/Vanguard/backend:/code python:3.12-slim "
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
                    f"docker compose -f docker-compose.production.yml build {services}"
                ),
                timeout=2400,
            )
            run(
                client,
                (
                    f"cd {shlex.quote(REMOTE_ROOT)} && "
                    f"docker compose -f docker-compose.production.yml up -d --force-recreate {services}"
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
            "backend/migrations/019_group_search_keyword_normalized.sql",
            "backend/migrations/020_keyword_trigger_review.sql",
            "frontend/src/api/acquisition.ts",
            "frontend/src/api/automation.ts",
            "frontend/src/api/groups.ts",
            "frontend/src/components/StatusTag.vue",
            "frontend/src/views/Automation.vue",
            "frontend/src/views/Groups.vue",
            "frontend/src/views/Keywords.vue",
        ]
        backup_dir = f"/root/Vanguard.file-backups/acquisition-automation-{timestamp}"
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
                    "docker run --rm -v /root/Vanguard/backend:/code python:3.12-slim "
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
                    f"docker compose -f docker-compose.production.yml build {services}"
                ),
                timeout=2400,
            )
            run(
                client,
                (
                    f"cd {shlex.quote(REMOTE_ROOT)} && "
                    "docker compose -f docker-compose.production.yml run --rm backend "
                    "env PYTHONPATH=/app python /app/scripts/apply_sql_migrations.py "
                    "--files 019_group_search_keyword_normalized.sql 020_keyword_trigger_review.sql"
                ),
                timeout=600,
            )
            run(
                client,
                (
                    f"cd {shlex.quote(REMOTE_ROOT)} && "
                    f"docker compose -f docker-compose.production.yml up -d --force-recreate {services}"
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
        backup_dir = f"/root/Vanguard.file-backups/scheduler-tasks-{timestamp}"
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
                    "docker run --rm -v /root/Vanguard/backend:/code python:3.12-slim "
                    "python -m py_compile /code/app/core/scheduler/tasks.py"
                ),
                timeout=180,
            )
            run(
                client,
                (
                    f"cd {shlex.quote(REMOTE_ROOT)} && "
                    "docker compose -f docker-compose.production.yml build celery-worker celery-beat"
                ),
                timeout=2400,
            )
            run(
                client,
                (
                    f"cd {shlex.quote(REMOTE_ROOT)} && "
                    "docker compose -f docker-compose.production.yml up -d --force-recreate celery-worker celery-beat"
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
        run(client, ensure_external_networks_command(), timeout=120, allow_fail=False)
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
