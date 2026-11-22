"""Release read snapshots without committing another operation's writes.

Import before opening application sessions. Tracking includes flushed ORM
changes and SQL executed through a session's Connection, without recording SQL
text or parameters. A caller must explicitly vouch for a legacy transaction
opened before these hooks were installed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from weakref import WeakKeyDictionary, ref

from sqlalchemy import event, inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.sql import visitors


class TransactionBoundaryBlocked(RuntimeError):
    """The current transaction cannot safely be released at a read boundary."""


@dataclass
class _WriteSet:
    orm_states: set[Any] = field(default_factory=set)
    orm_fields: dict[Any, set[str]] = field(default_factory=dict)
    deleted: bool = False
    other_sql: bool = False


_transactions: WeakKeyDictionary = WeakKeyDictionary()
_savepoints: WeakKeyDictionary = WeakKeyDictionary()
_PENDING_SQL = "vanguard_transaction_boundary_pending_sql"


def _read_statement(statement: Any, sql: str | None = None) -> bool:
    if getattr(statement, "is_select", False):
        return not any(getattr(item, "is_dml", False) for item in visitors.iterate(statement))
    # Transaction read-only controls are safe to commit; arbitrary text SQL,
    # including writable CTEs, cannot claim a read boundary by its first word.
    normalized = " ".join((sql if sql is not None else str(statement)).strip().rstrip(";").upper().split())
    return bool(re.fullmatch(
        r"SET(?: LOCAL)? TRANSACTION (?:ISOLATION LEVEL (?:REPEATABLE READ|READ COMMITTED|SERIALIZABLE),? )?READ ONLY"
        r"|SET LOCAL TRANSACTION_READ_ONLY\s*=\s*(?:ON|TRUE|1)", normalized,
    ))


def _writes(session: Session) -> _WriteSet | None:
    transaction = session.get_transaction()
    return _transactions.get(transaction) if transaction is not None else None


@event.listens_for(Session, "after_transaction_create")
def _created(session: Session, transaction: Any) -> None:
    if transaction.parent is None:
        _transactions[transaction] = _WriteSet(other_sql=bool(session.info.pop(_PENDING_SQL, False)))
    elif transaction.nested:
        writes = _writes(session)
        if writes is not None:
            _savepoints[transaction] = _WriteSet(
                set(writes.orm_states), {state: set(fields) for state, fields in writes.orm_fields.items()},
                writes.deleted, writes.other_sql,
            )


@event.listens_for(Session, "after_soft_rollback")
def _rolled_back(session: Session, previous: Any) -> None:
    if previous.nested and previous in _savepoints:
        root = session.get_transaction()
        if root is not None:
            _transactions[root] = _savepoints.pop(previous)
    if previous.parent is None:
        session.info.pop(_PENDING_SQL, None)


@event.listens_for(Session, "after_transaction_end")
def _ended(session: Session, transaction: Any) -> None:
    if transaction.parent is None:
        session.info.pop(_PENDING_SQL, None)


@event.listens_for(Session, "do_orm_execute")
def _execute(state: Any) -> None:
    if _read_statement(state.statement):
        return
    writes = _writes(state.session)
    if writes is None:
        state.session.info[_PENDING_SQL] = True
    else:
        writes.other_sql = True


@event.listens_for(Session, "after_flush")
def _flushed(session: Session, _context: Any) -> None:
    writes = _writes(session)
    if writes is not None:
        for value in (*session.new, *session.dirty, *session.deleted):
            state = inspect(value)
            writes.orm_states.add(state)
            writes.orm_fields.setdefault(state, set()).update(
                attribute.key for attribute in state.mapper.column_attrs
                if state.attrs[attribute.key].history.has_changes()
            )
        writes.deleted |= bool(session.deleted)


@event.listens_for(Session, "after_begin")
def _begun(session: Session, _transaction: Any, connection: Any) -> None:
    """Cover Core/driver SQL that bypasses Session.do_orm_execute."""
    connection._vanguard_boundary_owner = ref(session)
    if getattr(connection, "_vanguard_boundary_installed", False):
        return
    connection._vanguard_boundary_installed = True
    def before_cursor(_connection, _cursor, _statement, _parameters, context, _many):
        owner = getattr(_connection, "_vanguard_boundary_owner", None)
        current = owner() if owner is not None else None
        if current is None or current._flushing:
            return  # The ORM write set is collected by after_flush instead.
        writes = _writes(current)
        if writes is None:
            return
        compiled = getattr(context, "compiled", None)
        statement = getattr(compiled, "statement", None)
        if not _read_statement(statement, _statement):
            # Text SQL, stored procedures and DDL are deliberately unknown.
            writes.other_sql = True

    event.listen(connection, "before_cursor_execute", before_cursor)


def _pending_states(session: Session) -> tuple[set[Any], bool]:
    states = {inspect(value) for value in (*session.new, *session.dirty, *session.deleted)}
    deleted = bool(session.deleted)
    transaction = session.get_transaction()
    if transaction is not None:
        # SQLAlchemy 2 retains already flushed ORM changes in its rollback
        # snapshot. This also protects an explicitly vouched legacy transaction.
        for name in ("_new", "_dirty", "_deleted"):
            retained = getattr(transaction, name, None)
            if retained is None:
                raise TransactionBoundaryBlocked("unsupported_transaction_write_set")
            states.update(retained)
        deleted |= bool(transaction._deleted)
    return states, deleted


async def release_read_transaction(
    db: Any,
    *,
    allow_untracked_read_only: bool = False,
    allowed_objects: tuple[Any, ...] = (),
    allowed_object_fields: dict[Any, frozenset[str]] | None = None,
    allowed_setting_keys: tuple[str, ...] = (),
) -> bool:
    """Commit a proven read snapshot, or a caller's explicit scoped ORM claim.

    The default rejects every write. The optional allowlist is only for a
    boundary that owns those specific ORM changes; Core/text writes cannot be
    allowlisted. A blocked boundary neither commits nor rolls back caller work.
    Lightweight offline collectors do not own database connections.
    """
    if not isinstance(db, AsyncSession):
        return False
    session = db.sync_session
    transaction = session.get_transaction()
    if transaction is None:
        return False
    if session.in_nested_transaction():
        raise TransactionBoundaryBlocked("nested_transaction")
    writes = _writes(session)
    if writes is None and not allow_untracked_read_only:
        raise TransactionBoundaryBlocked("untracked_transaction")
    if (writes is not None and writes.other_sql) or session.info.get(_PENDING_SQL):
        raise TransactionBoundaryBlocked("pending_core_or_unknown_sql")
    states, deleted = _pending_states(session)
    if writes is not None:
        states.update(writes.orm_states)
        deleted |= writes.deleted
    if deleted:
        raise TransactionBoundaryBlocked("pending_deletion")
    allowed = {inspect(value) for value in allowed_objects}
    field_limits = {inspect(value): fields for value, fields in (allowed_object_fields or {}).items()}
    for state in states:
        value = state.obj()
        setting = (
            getattr(getattr(state, "mapper", None), "local_table", None) is not None
            and state.mapper.local_table.name == "system_setting"
            and (getattr(value, "key", None) if value is not None else (state.identity or (None,))[0])
                in allowed_setting_keys
        )
        if state not in allowed and not setting:
            raise TransactionBoundaryBlocked("pending_orm_write")
        if state in allowed and not state.persistent:
            raise TransactionBoundaryBlocked("uncommitted_scope_identity")
        if state in allowed or setting:
            changed_fields = set(writes.orm_fields.get(state, ())) if writes is not None else set()
            changed_fields.update(
                attribute.key for attribute in state.mapper.column_attrs
                if state.attrs[attribute.key].history.has_changes()
            )
            permitted = (frozenset({"key", "value", "created_at", "updated_at"}) if setting
                         else field_limits.get(state, frozenset()))
            if not changed_fields <= permitted:
                raise TransactionBoundaryBlocked("pending_unowned_scope_field")
    await db.commit()
    return True
