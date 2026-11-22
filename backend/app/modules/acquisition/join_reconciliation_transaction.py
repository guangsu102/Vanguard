"""Short database phases around reads of an already submitted join request."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from sqlalchemy import inspect, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database_transaction_boundary import TransactionBoundaryBlocked
from app.core.group.identity import telegram_group_id_aliases
from app.core.group.models import Group, GroupAccountMembership
from app.modules.acquisition.models import AutoJoinAttempt


@asynccontextmanager
async def transaction(db: AsyncSession) -> AsyncIterator[None]:
    """Commit/rollback only a phase that this reconciliation started itself.

    Some legacy local helpers commit their own changes. They may finish this
    phase early, but must never inherit and finish somebody else's transaction.
    """
    if db.in_transaction():
        raise TransactionBoundaryBlocked("join_reconciliation_requires_released_transaction")
    try:
        yield
        if db.in_transaction():
            await db.commit()
    except BaseException:
        if db.in_transaction():
            await db.rollback()
        raise


def account_snapshot(account: Any) -> Any:
    """Give the pool scalar credentials without retaining an attached ORM row."""
    state = inspect(account)
    values = {
        column.key: state.dict[column.key]
        for column in state.mapper.column_attrs
        if column.key in state.dict
    }
    config = account.api_config
    values["api_config"] = (
        SimpleNamespace(api_id=config.api_id, api_hash=config.api_hash)
        if config is not None
        else None
    )
    return SimpleNamespace(**values)


def _attempt_scope(attempt: AutoJoinAttempt) -> tuple[Any, ...]:
    return tuple(getattr(attempt, column.key) for column in inspect(AutoJoinAttempt).column_attrs)


def _group_scope(group: Group | None) -> tuple[Any, ...] | None:
    return (group.id, group.group_id, group.username) if group is not None else None


_MEMBERSHIP_FIELDS = (
    "id",
    "account_id",
    "group_id",
    "telegram_group_id",
    "status",
    "joined_at",
    "left_at",
    "last_checked_at",
    "review_status",
    "review_started_at",
    "review_deadline_at",
    "review_next_at",
    "updated_at",
)


async def membership_snapshots(
    db: AsyncSession, account_ids: set[int]
) -> dict[int, tuple[tuple[Any, ...], ...]]:
    """Snapshot account memberships once; compare only the resolved target later.

    Invite links can reveal the stable group identity only after the read. The
    scalar snapshot lets that result be checked against its original membership
    scope without keeping a transaction or an ORM object across the read.
    """
    rows = (
        await db.execute(
            select(*(getattr(GroupAccountMembership, name) for name in _MEMBERSHIP_FIELDS))
            .where(
                GroupAccountMembership.account_id.in_(account_ids),
            )
            .order_by(GroupAccountMembership.id)
        )
    ).all()
    return {
        account_id: tuple(tuple(row) for row in rows if row.account_id == account_id)
        for account_id in account_ids
    }


@dataclass(frozen=True)
class Claim:
    attempt_id: int
    account_id: int
    group_id: int | None
    telegram_group_id: int | None
    target_key: str | None
    target: str | int | None
    claimed_until: datetime
    attempt_scope: tuple[Any, ...]
    group_scope: tuple[Any, ...] | None
    memberships: tuple[tuple[Any, ...], ...]

    @classmethod
    def capture(
        cls,
        attempt: AutoJoinAttempt,
        group: Group | None,
        target: str | int | None,
        memberships: tuple[tuple[Any, ...], ...],
    ) -> "Claim":
        return cls(
            attempt.id,
            attempt.account_id,
            attempt.group_id,
            attempt.telegram_group_id,
            attempt.target_key,
            target,
            attempt.reconciliation_next_at,
            _attempt_scope(attempt),
            _group_scope(group),
            memberships,
        )

    async def reload(self, db: AsyncSession) -> tuple[AutoJoinAttempt, Group | None] | None:
        """Lock fresh facts and discard a lost claim or changed request/target."""
        attempt = await db.scalar(
            select(AutoJoinAttempt)
            .where(
                AutoJoinAttempt.id == self.attempt_id,
            )
            .with_for_update(of=AutoJoinAttempt)
            .execution_options(populate_existing=True)
        )
        if attempt is None or _attempt_scope(attempt) != self.attempt_scope:
            return None
        group = (
            await db.scalar(
                select(Group)
                .where(Group.id == self.group_id)
                .with_for_update(of=Group)
                .execution_options(populate_existing=True)
            )
            if self.group_id is not None
            else None
        )
        if _group_scope(group) != self.group_scope:
            return None
        if not await self.membership_current(db, self.group_id, self.telegram_group_id):
            return None
        return attempt, group

    async def membership_current(
        self, db: AsyncSession, group_id: int | None, telegram_group_id: int | None
    ) -> bool:
        """Fence leave/rejoin and another worker's membership confirmation."""
        aliases = telegram_group_id_aliases(telegram_group_id) if telegram_group_id else ()
        if group_id is None and not aliases:
            return True
        identity = or_(
            GroupAccountMembership.group_id == group_id if group_id is not None else False,
            GroupAccountMembership.telegram_group_id.in_(aliases) if aliases else False,
        )
        rows = (
            await db.execute(
                select(*(getattr(GroupAccountMembership, name) for name in _MEMBERSHIP_FIELDS))
                .where(
                    GroupAccountMembership.account_id == self.account_id,
                    identity,
                )
                .order_by(GroupAccountMembership.id)
                .with_for_update(of=GroupAccountMembership)
            )
        ).all()
        expected = tuple(
            row
            for row in self.memberships
            if (group_id is not None and row[2] == group_id) or row[3] in aliases
        )
        return tuple(tuple(row) for row in rows) == expected
