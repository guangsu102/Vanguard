"""Shared search-group occupancy, backed by durable requests and PostgreSQL locks."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, String, and_, case, cast, exists, func, literal, or_, select

from app.core.group.identity import telegram_group_id_aliases
from app.core.group.models import Group
from app.core.group.models import GroupAccountMembership as Member
from app.modules.acquisition.models import AutoJoinAttempt as Attempt


def active_request(now: datetime) -> Any:
    """Unknown sent results never expire merely because their timer has elapsed."""
    unresolved = Attempt.reconciliation_status.notin_(["confirmed", "resolved"])
    return or_(
        and_(
            Attempt.request_state == "reserved",
            or_(
                Attempt.reservation_expires_at.is_(None),
                Attempt.reservation_expires_at > now,
            ),
        ),
        and_(
            unresolved,
            or_(
                Attempt.request_state == "outcome_unknown",
                and_(
                    Attempt.request_state == "sent",
                    or_(
                        Attempt.status == "pending",
                        and_(Attempt.status == "success", Attempt.joined_at.is_(None)),
                    ),
                ),
            ),
        ),
    )


def raw_peer_id(value: Any) -> Any:
    return case(
        (value < -1_000_000_000_000, -value - literal(1_000_000_000_000, type_=BigInteger)),
        (value < 0, -value),
        else_=value,
    )


def group_occupied(now: datetime, *, exclude_account_id: int | None = None) -> Any:
    """Correlated predicate shared by selection, budgeting, and capacity displays."""
    peer = raw_peer_id(Group.group_id)
    member = (
        select(Member.id)
        .where(
            or_(Member.group_id == Group.id, raw_peer_id(Member.telegram_group_id) == peer),
            Member.status.in_(["joined", "pending", "leave_failed"]),
            Member.left_at.is_(None),
        )
        .correlate(Group)
    )
    keys = [
        literal("group:") + cast(Group.id, String),
        literal("username:") + func.lower(Group.username),
        literal("telegram:") + cast(peer, String),
        literal("telegram:") + cast(-peer, String),
        literal("telegram:") + cast(-peer - literal(1_000_000_000_000, type_=BigInteger), String),
    ]
    request = (
        select(Attempt.id)
        .where(
            or_(
                Attempt.group_id == Group.id,
                raw_peer_id(Attempt.telegram_group_id) == peer,
                func.lower(Attempt.group_username) == func.lower(Group.username),
                Attempt.target_key.in_(keys),
            ),
            active_request(now),
        )
        .correlate(Group)
    )
    if exclude_account_id is not None:
        member = member.where(Member.account_id != exclude_account_id)
        request = request.where(Attempt.account_id != exclude_account_id)
    return or_(exists(member), exists(request))


async def occupied_group_ids(db: Any, group_ids: list[int], now: datetime) -> set[int]:
    if not group_ids:
        return set()
    return set(
        (
            await db.scalars(
                select(Group.id).where(
                    Group.id.in_(group_ids),
                    group_occupied(now),
                )
            )
        ).all()
    )


@dataclass
class JoinTarget:
    group_ids: set[int] = field(default_factory=set)
    aliases: set[int] = field(default_factory=set)
    usernames: set[str] = field(default_factory=set)
    keys: set[str] = field(default_factory=set)


async def resolve_target(
    db: Any,
    *,
    group_id: int | None,
    telegram_group_id: int | None,
    group_username: str | None,
    target_key: str | None,
) -> JoinTarget:
    target = JoinTarget()
    if group_id is not None:
        target.group_ids.add(group_id)
    target.aliases.update(telegram_group_id_aliases(telegram_group_id))
    username = (group_username or "").strip().lstrip("@").lower()
    if username:
        target.usernames.add(username)
    if target_key:
        target.keys.add(target_key)
        kind, _, value = target_key.partition(":")
        if kind == "username" and value:
            target.usernames.add(value.lower())
        elif kind in {"group", "telegram"}:
            try:
                identity = int(value)
                if kind == "group":
                    target.group_ids.add(identity)
                else:
                    target.aliases.update(telegram_group_id_aliases(identity))
            except ValueError:
                pass
    predicates = []
    if target.group_ids:
        predicates.append(Group.id.in_(target.group_ids))
    if target.aliases:
        predicates.append(Group.group_id.in_(target.aliases))
    if target.usernames:
        predicates.append(func.lower(Group.username).in_(target.usernames))
    if predicates:
        rows = (
            await db.execute(
                select(Group.id, Group.group_id, Group.username).where(
                    or_(*predicates),
                )
            )
        ).all()
        for row in rows:
            target.group_ids.add(row.id)
            target.aliases.update(telegram_group_id_aliases(row.group_id))
            if row.username:
                target.usernames.add(row.username.lstrip("@").lower())
    target.keys.update(f"group:{value}" for value in target.group_ids)
    target.keys.update(f"telegram:{value}" for value in target.aliases)
    target.keys.update(f"username:{value}" for value in target.usernames)
    return target


async def lock_target(db: Any, target: JoinTarget) -> None:
    """Serialize check-and-reserve across processes, without adding a table.

    Acquire after account/config locks, before attempt locks. Locks last only
    until commit/rollback; the persisted request holds ownership during the RPC.
    Every caller acquires the sorted identity keys so aliases share a lock and
    overlapping targets cannot deadlock. SQLite is used only for unit tests.
    """
    if db.get_bind().dialect.name != "postgresql":
        return
    lock_ids = sorted(
        {
            int.from_bytes(
                hashlib.blake2b(
                    ("vanguard:shared-join:" + key).encode(),
                    digest_size=8,
                ).digest(),
                "big",
                signed=True,
            )
            for key in target.keys
        }
    )
    for lock_id in lock_ids:
        await db.execute(select(func.pg_advisory_xact_lock(literal(lock_id, type_=BigInteger))))


async def target_blocker(
    db: Any,
    target: JoinTarget,
    account_id: int,
    now: datetime,
    *,
    exclude_attempt_id: int | None = None,
    include_memberships: bool = True,
) -> str | None:
    members = []
    requests = []
    if target.group_ids:
        members.append(Member.group_id.in_(target.group_ids))
        requests.append(Attempt.group_id.in_(target.group_ids))
    if target.aliases:
        members.append(Member.telegram_group_id.in_(target.aliases))
        requests.append(Attempt.telegram_group_id.in_(target.aliases))
    if target.usernames:
        requests.append(func.lower(Attempt.group_username).in_(target.usernames))
    if target.keys:
        requests.append(Attempt.target_key.in_(target.keys))
    if include_memberships and members:
        owner = (
            await db.execute(
                select(Member.account_id, Member.status)
                .where(
                    or_(*members),
                    Member.status.in_(["joined", "pending", "leave_failed"]),
                    Member.left_at.is_(None),
                )
                .limit(1)
            )
        ).first()
        if owner is not None:
            if owner.status == "joined":
                return (
                    "already_joined"
                    if owner.account_id == account_id
                    else "already_joined_by_other_account"
                )
            return "join_target_occupied"
    if requests:
        query = select(Attempt.account_id, Attempt.request_state).where(
            or_(*requests),
            active_request(now),
        )
        if exclude_attempt_id is not None:
            query = query.where(Attempt.id != exclude_attempt_id)
        owner = (await db.execute(query.limit(1))).first()
        if owner is not None:
            if owner.account_id == account_id and owner.request_state != "reserved":
                return "join_outcome_reconciliation_required"
            return "join_target_occupied"
    return None
