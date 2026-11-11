"""Draft per-Work slice cost observations; no reservations or estimated savings."""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from typing import Any

TASK_PREFIX = "qualification.cost.work.v1."
SLICE_PREFIX = "qualification.cost.slice.v1."
COVERAGE_PREFIX = "qualification.cost.coverage.v1."
SLA_COVERAGE_PREFIX = "qualification.ready.sla.coverage.v1."
RECONCILE_CURSOR = "qualification.cost.reconcile.cursor.v1"
MAX_ROWS = 4096
RETIRE_ROWS = 16
CAPACITY_PRESSURE = "qualification.telemetry.capacity_pressure"
RETENTION_DAYS = 7
# A full ledger cannot retire anything new until rows age past retention, so
# rerunning the retirement scan on every observation event only re-scans
# history (observed as repeated statement timeouts on production). One attempt
# per account per window, serialized by the per-account advisory lock.
RETIRE_SCAN_COOLDOWN_SECONDS = 900


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _data(row: Any) -> dict:
    try:
        return json.loads(row.value or "{}") if row is not None else {}
    except (TypeError, ValueError):
        return {}


def _date(value: str | datetime | None) -> datetime | None:
    try:
        from datetime import timezone
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value) if value else None
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed and parsed.tzinfo else parsed
    except (TypeError, ValueError):
        return None


def _save(row: Any, value: dict) -> None:
    row.value = json.dumps(value, sort_keys=True, separators=(",", ":"))


def task_key(event: dict) -> str:
    return TASK_PREFIX+str(event["account_id"])+"."+_hash([event["work_id"], event["revision"],
        event.get("membership_id"), event.get("membership_incarnation"), event["scope_hash"]])


def slice_key(event: dict) -> str:
    return SLICE_PREFIX+str(event["account_id"])+"."+_hash(event["slice_token"])


async def _lock(db: Any, account: int) -> None:
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy import text
        await db.execute(text("SELECT pg_advisory_xact_lock(572911,:account)"), {"account": account})


async def _rows(db: Any, account: int) -> list:
    from sqlalchemy import or_, select
    from app.core.settings_models import SystemSetting
    return list((await db.scalars(select(SystemSetting).where(or_(
        SystemSetting.key.startswith(TASK_PREFIX+str(account)+"."),
        SystemSetting.key.startswith(SLICE_PREFIX+str(account)+"."),
    )).order_by(SystemSetting.key).limit(MAX_ROWS+1))).all())


async def note_gap(db: Any, account: int, now: datetime, reason: str) -> None:
    from app.core.settings_models import SystemSetting
    row = await db.get(SystemSetting, COVERAGE_PREFIX+str(account))
    value = _data(row)
    value.setdefault("started_at", now.isoformat())
    # Capacity/failure is conservative for an entire following window. The
    # existing pending rows survive; no missing cost is replaced with zero.
    value["unknown_until"] = (now+timedelta(days=1)).isoformat()
    value["last_error"] = reason
    if row is None:
        row = SystemSetting(key=COVERAGE_PREFIX+str(account), value="{}")
        db.add(row)
    _save(row, value)
    await db.flush()


def _json_text_field(db: Any, column: Any, field: str, *, type_only: bool = False) -> Any:
    """Filter JSON inside SQL; malformed text remains occupied, never retired.

    Only a bounded candidate group is decoded by Python. SQL is a prefilter;
    the existing Python retirement proof is still checked before any delete.
    """
    from sqlalchemy import case, cast, func

    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import JSONB

        safe = case((func.pg_input_is_valid(column, "jsonb"), column), else_="null")
        value = cast(safe, JSONB)[field]
        return func.jsonb_typeof(value) if type_only else value.astext
    if db.get_bind().dialect.name == "sqlite":
        safe = case((func.json_valid(column), column), else_="null")
        function = func.json_type if type_only else func.json_extract
        return function(safe, "$." + field)
    raise ValueError("telemetry_json_dialect_unsupported")


def _postgres_json_rows(scope: Any) -> Any:
    """Parse each scoped text once per SQL statement, without a cross-write cache."""
    from sqlalchemy import case, cast, func, select
    from sqlalchemy.dialects.postgresql import JSONB
    from app.core.settings_models import SystemSetting

    checked = select(SystemSetting.key, SystemSetting.value,
        func.pg_input_is_valid(SystemSetting.value, "jsonb").label("valid"),
    ).where(scope).cte("telemetry_json_checked").prefix_with("MATERIALIZED", dialect="postgresql")
    return select(checked.c.key, checked.c.value, checked.c.valid,
        cast(case((checked.c.valid, checked.c.value), else_="null"), JSONB).label("payload"),
    ).cte("telemetry_json_payload").prefix_with("MATERIALIZED", dialect="postgresql")


async def _retire_scan_allowed(db: Any, account: int, now: datetime) -> bool:
    from app.core.settings_models import SystemSetting
    marker = await db.get(SystemSetting, COVERAGE_PREFIX+str(account))
    return (_date(_data(marker).get("retire_scan_not_before")) or datetime.min) < now


def _ordered_first_fit(candidates: list, slots: int = RETIRE_ROWS) -> tuple[list, dict]:
    """Ordered first-fit whole groups within the shared slot budget.

    Every group costs its own row plus one slot per related slice reference;
    an oversized-for-remaining group is skipped without blocking later ones.
    """
    counts = {row.key: row.related_count for row in candidates}
    selected, remaining = [], slots
    for row in candidates:
        size = 1 + counts.get(row.key, 0)
        if size <= remaining:
            selected.append(row)
            remaining -= size
    return selected, counts


async def _mark_retire_scan(db: Any, account: int, now: datetime) -> None:
    """Bound the next retirement scan attempt regardless of its outcome."""
    from app.core.settings_models import SystemSetting
    row = await db.get(SystemSetting, COVERAGE_PREFIX+str(account))
    value = _data(row)
    value["retire_scan_not_before"] = (now+timedelta(seconds=RETIRE_SCAN_COOLDOWN_SECONDS)).isoformat()
    if row is None:
        row = SystemSetting(key=COVERAGE_PREFIX+str(account), value="{}")
        db.add(row)
    _save(row, value)
    await db.flush()


async def _retirement_json_ambiguous(db: Any, scope: Any) -> bool:
    """Native JSON cannot silently hide a Python-visible unresolved reference."""
    from sqlalchemy import case, cast, func, or_, select, true
    from app.core.settings_models import SystemSetting

    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        native = _postgres_json_rows(scope)
        shape = func.jsonb_typeof(native.c.payload)
        reference_type = func.jsonb_typeof(native.c.payload["task_key"])
        return await db.scalar(select(native.c.key).where(or_(
            native.c.valid.is_not(True), shape != "object",
            reference_type.is_not(None) & reference_type.not_in(("null", "string", "text")),
        )).limit(1)) is not None
    valid = func.json_valid(SystemSetting.value)
    if await db.scalar(select(SystemSetting.key).where(scope, valid.is_not(True)).limit(1)) is not None:
        # Python accepts values that JSONB rejects, such as an escaped NUL in
        # another field. Its task_key may still be an unresolved reference.
        return True
    safe = case((valid, SystemSetting.value), else_="null")
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import JSONB

        payload = cast(safe, JSONB)
        shape, reference_type = func.jsonb_typeof(payload), func.jsonb_typeof(payload["task_key"])
    else:
        shape, reference_type = func.json_type(safe), func.json_type(safe, "$.task_key")
    if await db.scalar(select(SystemSetting.key).where(scope, or_(
        shape != "object",
        reference_type.is_not(None) & reference_type.not_in(("null", "string", "text")),
    )).limit(1)) is not None:
        return True
    if dialect == "sqlite":
        # SQLite's scalar extract uses the first duplicate key; Python uses
        # the last. Retain all rows when that reference interpretation differs.
        safe = case((func.json_valid(SystemSetting.value), SystemSetting.value), else_="null")
        entries = func.json_each(safe).table_valued("key", "value")
        duplicate = select(SystemSetting.key).select_from(SystemSetting).join(entries, true()).where(
            scope, entries.c.key == "task_key",
        ).group_by(SystemSetting.key).having(func.count() > 1).limit(1)
        if await db.scalar(duplicate) is not None:
            return True
    return False


async def _capacity(db: Any, account: int, needed: int, now: datetime) -> bool:
    from sqlalchemy import and_, case, delete, func, or_, select
    from app.core.settings_models import SystemSetting
    # The measurement session disables autoflush. Count the exact transaction
    # contents, including prior admissions/deletes, without decoding history.
    await db.flush()
    count = await db.scalar(select(func.count()).select_from(SystemSetting).where(or_(
        SystemSetting.key.startswith(TASK_PREFIX+str(account)+"."),
        SystemSetting.key.startswith(SLICE_PREFIX+str(account)+"."),
    )))
    if count+needed > MAX_ROWS-4:
        db.info[CAPACITY_PRESSURE] = True
    if count+needed <= MAX_ROWS:
        return True
    if count > MAX_ROWS:
        # A truncated history cannot prove that every related slice is settled.
        await note_gap(db, account, now, "cost_capacity_exhausted")
        return False
    scope = or_(
        SystemSetting.key.startswith(TASK_PREFIX+str(account)+"."),
        SystemSetting.key.startswith(SLICE_PREFIX+str(account)+"."),
    )
    # A full ledger cannot retire anything new until rows age past retention;
    # re-running the scan on every observation event only re-scans history.
    if not await _retire_scan_allowed(db, account, now):
        return False
    if await _retirement_json_ambiguous(db, scope):
        await _mark_retire_scan(db, account, now)
        await note_gap(db, account, now, "cost_retirement_json_unknown")
        return False
    postgres = db.get_bind().dialect.name == "postgresql"
    table = SystemSetting
    key_column, value_column, row_scope = SystemSetting.key, SystemSetting.value, scope
    terminal = _json_text_field(db, SystemSetting.value, "terminal")
    terminal_at = _json_text_field(db, SystemSetting.value, "terminal_at")
    reference = _json_text_field(db, SystemSetting.value, "task_key")
    finished = _json_text_field(db, SystemSetting.value, "finished_at")
    known = _json_text_field(db, SystemSetting.value, "budget_known")
    finished_type = _json_text_field(db, SystemSetting.value, "finished_at", type_only=True)
    terminal_type = _json_text_field(db, SystemSetting.value, "terminal", type_only=True)
    # JSON booleans surface as text on PostgreSQL (astext) and as integers on
    # SQLite (json_extract).
    true_value = "true" if postgres else 1
    settled = and_(finished_type == "text", finished != "", known == true_value)
    references = select(reference.label("task_key"), func.count().label("rows"),
        func.sum(case((settled, 0), else_=1)).label("unsettled"),
    ).select_from(table).where(row_scope, reference.is_not(None)).group_by(reference).cte("cost_retirement_references")
    # Filter protected/oversized groups BEFORE the bounded candidate limit.
    candidates = list((await db.execute(select(key_column, value_column,
        func.coalesce(references.c.rows, 0).label("related_count"),
    ).select_from(table).outerjoin(references, references.c.task_key == key_column).where(
        key_column.startswith(TASK_PREFIX+str(account)+"."),
        terminal == true_value, terminal_type == "true",
        terminal_at < (now-timedelta(days=RETENTION_DAYS)).isoformat(),
        func.coalesce(references.c.unsettled, 0) == 0,
        func.coalesce(references.c.rows, 0) < RETIRE_ROWS,
    ).order_by(key_column).limit(RETIRE_ROWS))).all())
    # Count ALL references in the account, including malformed non-slice
    # reference rows. Native JSON matching also sees escaped task-key strings.
    selected, counts = _ordered_first_fit(candidates)
    selected_keys = [row.key for row in selected]
    related_rows = list((await db.execute(select(key_column, value_column).select_from(table).where(
        row_scope, reference.in_(selected_keys),
    ).order_by(key_column).limit(RETIRE_ROWS+1))).all()) if selected_keys else []
    if len(related_rows) != sum(counts.get(key, 0) for key in selected_keys):
        await _mark_retire_scan(db, account, now)
        await note_gap(db, account, now, "cost_capacity_exhausted")
        return False
    retired = []
    related_by_task = {}
    values = {row.key: _data(row) for row in [*selected, *related_rows]}
    for row in related_rows:
        related_by_task.setdefault(values[row.key].get("task_key"), []).append(row)
    # Only a settled terminal whole Work can retire, together with all of its
    # slices. A started-but-unfinished Work or slice never ages out silently.
    for row in selected:
        value = values[row.key]
        if (row.key.startswith(TASK_PREFIX) and value.get("terminal") is True
                and (_date(value.get("terminal_at")) or datetime.max) < now-timedelta(days=RETENTION_DAYS)):
            related = related_by_task.get(row.key, [])
            if len(retired)+len(related)+1 <= RETIRE_ROWS and all(values[item.key].get("finished_at") and values[item.key].get("budget_known") for item in related):
                retired.extend([row, *related])
                # Small headroom avoids rescanning history for every Work in a
                # census microstep. A settled task and its slices stay atomic.
                if len(retired) >= RETIRE_ROWS:
                    break
    unique = {row.key: row for row in retired}
    if unique:
        await db.execute(delete(SystemSetting).where(SystemSetting.key.in_(unique)).execution_options(
            synchronize_session="fetch"))
    allowed = count-len(unique)+needed <= MAX_ROWS
    await _mark_retire_scan(db, account, now)
    if not allowed:
        await note_gap(db, account, now, "cost_capacity_exhausted")
    return allowed


async def _get_slice(db: Any, event: dict, now: datetime) -> tuple[Any, dict] | tuple[None, None]:
    from app.core.settings_models import SystemSetting
    account = event["account_id"]
    await _lock(db, account)
    key, whole_key = slice_key(event), task_key(event)
    row, whole = await db.get(SystemSetting, key), await db.get(SystemSetting, whole_key)
    if not await _capacity(db, account, int(row is None)+int(whole is None), now):
        return None, None
    if whole is None:
        whole = SystemSetting(key=whole_key, value="{}")
        db.add(whole)
        _save(whole, {"account_id": account, "work_id": event["work_id"], "revision": event["revision"],
            "membership_id": event.get("membership_id"), "membership_incarnation": event.get("membership_incarnation"),
            "scope_hash": event["scope_hash"], "task_demand": event.get("task_demand", {
                "task_complexity": "unknown", "complexity_source": "independent_whole_work_demand_unavailable"}),
            "first_observed_at": now.isoformat(), "terminal": False})
    if row is None:
        row = SystemSetting(key=key, value="{}")
        db.add(row)
        value = {"task_key": whole_key, "token_hash": _hash(event["slice_token"]),
            "account_id": account, "kind": event.get("slice_kind"), "phase": event.get("phase"),
            "complexity": event.get("complexity", "unknown"), "started_at": None,
            "finished_at": None, "sdk_entered": None, "budget_charged": None,
            "budget_known": False, "invalid": False, "failed": None}
    else:
        value = _data(row)
        if value.get("task_key") != whole_key:
            value["invalid"] = True
            await note_gap(db, account, now, "slice_identity_conflict")
    return row, value


async def observe_work(db: Any, event: dict, now: datetime) -> None:
    """Include a never-started Work; absence of slices is unknown, not zero."""
    from app.core.settings_models import SystemSetting
    await _lock(db, event["account_id"])
    key = task_key(event)
    if await db.get(SystemSetting, key) is not None:
        return
    if not await _capacity(db, event["account_id"], 1, now):
        return
    row = SystemSetting(key=key, value="{}")
    db.add(row)
    _save(row, {"account_id": event["account_id"], "work_id": event["work_id"], "revision": event["revision"],
        "membership_id": event.get("membership_id"), "membership_incarnation": event.get("membership_incarnation"),
        "scope_hash": event["scope_hash"], "task_demand": event.get("task_demand", {
            "task_complexity": "unknown", "complexity_source": "independent_whole_work_demand_unavailable"}),
        "first_observed_at": now.isoformat(), "terminal": False})
    await db.flush()


async def record_started(db: Any, event: dict, now: datetime) -> None:
    row, value = await _get_slice(db, event, now)
    if row is None:
        return
    started = _date(event.get("slice_started_at"))
    if started is None or started > now:
        value["invalid"] = True
    elif value.get("started_at") is None:
        value["started_at"] = started.isoformat()
    elif _date(value["started_at"]) != started:
        value["invalid"] = True
    _save(row, value)
    await db.flush()


async def _budget(db: Any, event: dict) -> tuple[bool, int | None, str]:
    from app.core.account.control_models import AccountBudgetReservation
    ledger_id = event.get("ledger_id")
    if not ledger_id:
        if event.get("reservation_not_taken") is True and event.get("sdk_entered") == 0:
            return True, 0, "proved_no_reservation"
        return False, None, "ledger_reference_missing"
    row = await db.get(AccountBudgetReservation, ledger_id)
    if row is None or row.account_id != event["account_id"] or row.workload != event.get("slice_kind"):
        return False, None, "ledger_identity_unknown"
    if row.state not in {"settled", "expired", "released"}:
        return False, None, "ledger_unsettled"
    if row.reason == "consumption_evidence_missing":
        return False, None, "consumption_evidence_missing"
    return True, row.consumed_reads, "existing_budget_ledger"


async def record_finished(db: Any, event: dict, now: datetime) -> None:
    row, value = await _get_slice(db, event, now)
    if row is None:
        return
    finished = _date(event.get("slice_finished_at"))
    sdk = event.get("sdk_entered")
    if finished is None or finished > now or type(sdk) is not int or sdk < 0:
        value["invalid"] = True
    elif value.get("finished_at") is not None and (value["finished_at"] != finished.isoformat() or value.get("sdk_entered") != sdk):
        value["invalid"] = True
    else:
        value.update(finished_at=finished.isoformat(), sdk_entered=sdk, failed=event.get("failed") is True,
            ledger_id=event.get("ledger_id"), reservation_not_taken=event.get("reservation_not_taken") is True,
            phase=event.get("phase"), complexity=event.get("complexity", "unknown"))
        known, charged, reason = await _budget(db, event)
        value.update(budget_known=known, budget_charged=charged, budget_reason=reason)
    _save(row, value)
    await db.flush()


async def refresh_business_state(db: Any, event: dict, now: datetime) -> None:
    from app.core.settings_models import SystemSetting
    from app.modules.acquisition.models import QualificationWork
    row = await db.get(SystemSetting, task_key(event))
    if row is None:
        return
    work = await db.get(QualificationWork, event["work_id"])
    value = _data(row)
    if (work is None or work.revision != event["revision"] or work.account_id != event["account_id"]
            or work.membership_id != event.get("membership_id") or _hash(work.scope_key) != event["scope_hash"]
            or (work.membership_joined_at.isoformat() if work.membership_joined_at else None) != event.get("membership_incarnation")):
        # A changed generation is an unresolved observation, not zero cost.
        value["terminal"] = False
        value["business_state"] = "generation_changed_or_missing"
    else:
        value["business_state"] = work.state
        value["terminal"] = work.state in {"DONE", "STALE"}
        if value["terminal"]:
            value.setdefault("terminal_at", now.isoformat())
    _save(row, value)


async def reconcile_pending(db: Any, accounts: set[int], now: datetime, *, limit: int = 16) -> None:
    """A durable bounded page of eventual settlements; never charge again."""
    from sqlalchemy import or_, select
    from app.core.settings_models import SystemSetting
    if not accounts:
        return
    limit = max(1, min(16, int(limit)))
    for account in sorted(accounts):
        await _lock(db, account)
    cursor = await db.get(SystemSetting, RECONCILE_CURSOR)
    state = _data(cursor)
    cohort = _hash(sorted(accounts))
    after = state.get("after", "") if state.get("cohort") == cohort else ""
    if not isinstance(after, str) or len(after) > 256:
        after = ""
    query = select(SystemSetting).where(or_(*(
        SystemSetting.key.startswith(SLICE_PREFIX+str(account)+".") for account in sorted(accounts)
    )), SystemSetting.key > after).order_by(SystemSetting.key).limit(limit)
    rows = list((await db.scalars(query)).all())
    for row in rows:
        value = _data(row)
        if (value.get("finished_at") and not value.get("budget_known")
                and value.get("budget_reason") == "ledger_unsettled"):
            known, charged, reason = await _budget(db, {**value, "slice_kind": value.get("kind")})
            value.update(budget_known=known, budget_charged=charged, budget_reason=reason)
            _save(row, value)
    if cursor is None:
        cursor = SystemSetting(key=RECONCILE_CURSOR, value="{}")
        db.add(cursor)
    _save(cursor, {"cohort": cohort, "after": rows[-1].key if len(rows) == limit else "",
        "last_page_at": now.isoformat()})
    await db.flush()


def project(rows: list[dict], *, coverage_complete: bool) -> dict:
    tasks = {row["_key"]: row for row in rows if row.get("_key", "").startswith(TASK_PREFIX)}
    slices = [row for row in rows if row.get("_key", "").startswith(SLICE_PREFIX)]
    results = []
    for key, task in tasks.items():
        selected = [row for row in slices if row.get("task_key") == key]
        sdk_known = bool(selected) and all(row.get("started_at") and row.get("finished_at")
            and type(row.get("sdk_entered")) is int and not row.get("invalid") for row in selected)
        budget_known = sdk_known and all(row.get("budget_known") is True for row in selected)
        results.append({"work_id": task["work_id"], "revision": task["revision"],
            "membership_incarnation": task.get("membership_incarnation"), "scope_hash": task["scope_hash"],
            "task_demand": task.get("task_demand", {"task_complexity": "unknown", "complexity_source": "unavailable"}),
            "slice_complexities": sorted({str(row.get("complexity", "unknown")) for row in selected}),
            "business_state": task.get("business_state"), "terminal": task.get("terminal") is True,
            "slices": len(selected), "open_slices": sum(not row.get("finished_at") for row in selected),
            "unknown_sdk_slices": sum(not row.get("started_at") or not row.get("finished_at") or row.get("invalid") for row in selected),
            "failed_slices": sum(row.get("failed") is True for row in selected),
            "sdk_entered": sum(row["sdk_entered"] for row in selected) if coverage_complete and sdk_known else None,
            "known_sdk_entered": sum(row.get("sdk_entered") or 0 for row in selected),
            "budget_charged": sum(row["budget_charged"] for row in selected) if coverage_complete and budget_known else None,
            "known_budget_charged": sum(row["budget_charged"] for row in selected
                if row.get("budget_known") is True and type(row.get("budget_charged")) is int),
            "unknown_budget_slices": sum(not row.get("budget_known") for row in selected),
            "cost_complete": coverage_complete and sdk_known and budget_known and task.get("terminal") is True})
    return {"tasks": results, "coverage_complete": coverage_complete,
        "matching_scope": "account_work_revision_membership_incarnation_scope;unique_readwork_token_slices",
        "matched_task_savings": None, "net_savings": None,
        "comparison_status": "no_equivalent_linked_baseline;cannot_validate_30_percent_savings",
        "sdk_and_budget_are_distinct": True}


async def summary(db: Any, account: int, now: datetime, *, coverage_complete: bool) -> dict:
    from app.core.settings_models import SystemSetting
    with db.no_autoflush:
        marker = await db.get(SystemSetting, COVERAGE_PREFIX+str(account))
        data = _data(marker)
        if (_date(data.get("unknown_until")) or datetime.min) >= now:
            coverage_complete = False
        rows = await _rows(db, account)
    if len(rows) > MAX_ROWS:
        coverage_complete = False
    return project([{**_data(row), "_key": row.key} for row in rows], coverage_complete=coverage_complete)


def _coverage_date(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        from datetime import timezone
        return datetime.fromtimestamp(value, timezone.utc).replace(tzinfo=None)
    return _date(value)


async def cost_report(db: Any, account_id: int, now: datetime, *, since: datetime | None = None,
                      sdk_coverage: dict | None = None) -> dict:
    """Read-only whole-Work cohort; missing transport/SDK proof stays null.

    sdk_coverage is the same-account fixed_read_metrics_v1 read-only snapshot,
    supplied by the collector/API. This function never opens its mmap or issues
    Telegram operations. It includes all slices of each observed cohort Work.
    """
    from app.core.settings_models import SystemSetting
    now = _date(now)
    if now is None:
        raise ValueError("valid now and since are required")
    since = _date(since) if since is not None else now-timedelta(hours=24)
    if now is None or since is None:
        raise ValueError("valid now and since are required")
    if since > now:
        raise ValueError("since must not exceed now")
    with db.no_autoflush:
        marker = _data(await db.get(SystemSetting, SLA_COVERAGE_PREFIX+str(account_id)))
        cost_marker = _data(await db.get(SystemSetting, COVERAGE_PREFIX+str(account_id)))
        rows = await _rows(db, account_id)
    reasons = []
    confirmed = _date(marker.get("source_confirmed_through"))
    observed = _date(marker.get("source_observed_at"))
    end = min(now, confirmed) if confirmed is not None else now
    if confirmed is None or confirmed > now:
        reasons.append("source_confirmed_watermark_missing_or_future")
    if not marker.get("expected_source_counts") or marker.get("source_inventory_verified") is not True:
        reasons.append("source_inventory_unverified")
    if observed is None or not 0 <= (now-observed).total_seconds() <= 60:
        reasons.append("source_heartbeat_stale")
    if marker.get("source_pending_events") != 0 or type(marker.get("source_pending_events")) is not int:
        reasons.append("source_events_pending")
    if (_date(marker.get("started_at")) or datetime.max) > since:
        reasons.append("observation_window_not_covered")
    if marker.get("denominator_loss") or marker.get("complete_override") is False:
        reasons.append("source_transport_loss")
    if any((_date(gap.get("start")) or datetime.min) < end
           and (_date(gap.get("end")) or datetime.max) > since for gap in marker.get("gaps", [])):
        reasons.append("source_transport_gap")
    if (_date(cost_marker.get("unknown_until")) or datetime.min) >= now or len(rows) > MAX_ROWS:
        reasons.append("cost_ledger_gap")
    sdk = sdk_coverage or {}
    sdk_start, sdk_end = _coverage_date(sdk.get("window_start")), _coverage_date(sdk.get("window_end"))
    sdk_confirmed = _coverage_date(sdk.get("sources_confirmed_through"))
    if not (type(sdk.get("account_id")) is int and sdk["account_id"] == account_id
            and sdk.get("source") == "fixed_read_metrics_v1" and sdk.get("measurement_version") == "sdk_entry_v2"
            and sdk.get("complete") is True and sdk_start is not None and sdk_start <= since
            and sdk_end is not None and sdk_confirmed is not None and sdk_confirmed >= sdk_end):
        reasons.append("sdk_coverage_unverified")
    elif sdk_end <= now:
        end = min(end, sdk_end)
    else:
        reasons.append("sdk_window_in_future")
    if end <= since:
        reasons.append("no_confirmed_observation_window")
    raw = [{**_data(row), "_key": row.key} for row in rows]
    cohort = {row["_key"] for row in raw if row["_key"].startswith(TASK_PREFIX)
        and since <= (_date(row.get("first_observed_at")) or datetime.min) <= end}
    selected = [row for row in raw if row["_key"] in cohort or row.get("task_key") in cohort]
    # Future slices/state cannot retroactively make the closed report complete.
    for row in selected:
        if row["_key"].startswith(SLICE_PREFIX) and ((_date(row.get("finished_at")) or datetime.max) > end):
            row["finished_at"] = None
        if row["_key"].startswith(TASK_PREFIX) and ((_date(row.get("terminal_at")) or datetime.max) > end):
            row["terminal"] = False
    result = project(selected, coverage_complete=not reasons)
    result.update(account_id=account_id, observed_at=now.isoformat(), window_start=since.isoformat(),
        window_end=end.isoformat(), requested_window_end=now.isoformat(),
        report_lag_seconds=max(0, (now-end).total_seconds()), incomplete_reasons=sorted(set(reasons)),
        source_coverage=marker, sdk_coverage_verified=not any(reason in reasons for reason in (
            "sdk_coverage_unverified", "sdk_window_in_future")),
        cohort="whole_work_first_observed_in_window;all_linked_slices_included;pending_work_retained")
    return result


async def cost_report_summary(db: Any, account_id: int, now: datetime, *, since: datetime | None = None,
                              sdk_coverage: dict | None = None, limit: int = 20) -> dict:
    """Bounded API projection with explicit truncation and all-Work totals."""
    limit = max(0, min(20, int(limit)))
    result = await cost_report(db, account_id, now, since=since, sdk_coverage=sdk_coverage)
    tasks = result.pop("tasks")
    marker = result.pop("source_coverage", {})
    complete_tasks = sum(row["cost_complete"] is True for row in tasks)
    result["source_coverage"] = {key: marker.get(key) for key in (
        "started_at", "source_observed_at", "source_confirmed_through", "expected_source_counts",
        "active_source_counts", "source_inventory_verified", "source_pending_events", "source_inventory_reason")}
    result["source_coverage"]["gap_count"] = len(marker.get("gaps", []))
    result["aggregate"] = {"observed_whole_work": len(tasks), "complete_cost_work": complete_tasks,
        "pending_work": sum(row["terminal"] is not True for row in tasks),
        "unknown_cost_work": len(tasks)-complete_tasks,
        "known_sdk_entered": sum(row["known_sdk_entered"] for row in tasks),
        "known_budget_charged": sum(row["known_budget_charged"] for row in tasks),
        "sdk_entered": sum(row["sdk_entered"] for row in tasks) if tasks and complete_tasks == len(tasks) else None,
        "budget_charged": sum(row["budget_charged"] for row in tasks) if tasks and complete_tasks == len(tasks) else None}
    result.update(tasks=tasks[:limit], returned_tasks=min(len(tasks), limit), total_tasks=len(tasks),
        tasks_limit=limit, tasks_truncated=len(tasks) > limit)
    return result
