"""Bounded per-operation read costs, separate from Telegram permission/budgets."""

from __future__ import annotations

import asyncio
import json
import math
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

KINDS = ("delivery", "two_minute", "one_hour", "twenty_four_hour", "daily")
MIN_SAMPLES = 8
COST_VERSION = "bounded-read-path-v2"
FORECAST_WINDOW = 6 * 3600
MAX_SAMPLES = 200
RETENTION = 7 * 86400


@dataclass
class ReadCost:
    account_id: int
    kind: str
    reads: int = 0
    complete: bool = False
    active: bool = True


_current: ContextVar[ReadCost | None] = ContextVar("ad_operation_read_cost", default=None)


def charge_reads(account_id: int, reads: int, lane: str) -> None:
    meter = _current.get()
    if meter is not None and meter.active and meter.account_id == account_id:
        expected = "ad" if meter.kind == "delivery" else "survival"
        if lane == expected:
            meter.reads += reads


def key(account_id: int, kind: str) -> str:
    return f"vanguard:rpc:operation_cost:{account_id}:{kind}"


async def _save(meter: ReadCost) -> None:
    from app.core.redis import get_redis

    cache = await get_redis()
    async with cache.pipeline(transaction=True) as pipe:
        name = key(meter.account_id, meter.kind)
        pipe.lpush(
            name, json.dumps({"at": time.time(), "reads": meter.reads, "complete": meter.complete, "path": COST_VERSION})
        )
        pipe.ltrim(name, 0, MAX_SAMPLES - 1)
        pipe.expire(name, RETENTION)
        await pipe.execute()


@asynccontextmanager
async def measure(account_id: int, kind: str):
    meter = ReadCost(account_id, kind)
    token = _current.set(meter)
    try:
        yield meter
    finally:
        meter.active = False
        _current.reset(token)
        if kind in KINDS and meter.reads:
            try:
                await asyncio.wait_for(_save(meter), timeout=1)
            except Exception:
                # Optional measurement must never fail or retry a delivery.
                pass


def estimate(samples: list[Any], fallback: int, now: float, *, path: str | None = None) -> tuple[int, int, bool]:
    valid = []
    for raw in samples:
        try:
            row = json.loads(raw)
            if (
                type(row["reads"]) is int
                and 0 < row["reads"] <= 10000
                and type(row["complete"]) is bool
                and now - (FORECAST_WINDOW if path else RETENTION) <= float(row["at"]) <= now
                and (path is None or row.get("path") == path)
            ):
                valid.append(row)
        except (ValueError, TypeError, KeyError):
            continue
    successes = sorted(row["reads"] for row in valid if row["complete"])
    if len(successes) < (MIN_SAMPLES if path else 20):
        return fallback, len(successes), False
    # Include the reads spent by unsuccessful attempts in the per-success cost.
    cost = max(
        successes[math.ceil(len(successes) * 0.9) - 1],
        math.ceil(sum(row["reads"] for row in valid) / len(successes)),
    )
    return max(1, cost + (max(1, math.ceil(cost * 0.15)) if path else 0)), len(successes), True


async def operation_costs(account_id: int) -> dict[str, Any]:
    from app.core.redis import get_redis

    result = {
        "delivery_read_cost": 6,
        "survival_read_cost": 10,
        "daily_review_read_cost": 10,
        "read_cost_source": "conservative_estimate",
        "read_cost_samples": {},
        "read_cost_path": COST_VERSION,
        "renewal_read_cost": 12,
    }
    try:
        cache = await get_redis()
        async with asyncio.timeout(1):
            rows = await asyncio.gather(
                *(cache.lrange(key(account_id, kind), 0, MAX_SAMPLES - 1) for kind in KINDS)
            )
        estimates = [
            estimate(samples, 6 if kind == "delivery" else 10, time.time(), path=COST_VERSION)
            for kind, samples in zip(KINDS, rows, strict=True)
        ]
        from app.core.account.read_work import sample_key
        renewal = await cache.lrange(sample_key(account_id, "renewal"), 0, MAX_SAMPLES - 1)
        normalized = []
        for raw in renewal:
            try:
                item = json.loads(raw)
                normalized.append(json.dumps({**item, "complete": item.get("outcome") in {"allowed", "trial"}}))
            except (ValueError, TypeError):
                continue
        result["renewal_read_cost"] = estimate(normalized, 12, time.time(), path=COST_VERSION)[0]
        result["delivery_read_cost"] = estimates[0][0]
        # New trials use the same single cycle reader as mature advertisements.
        # Retain old checkpoint sample counts for audit, not future workload.
        result["survival_read_cost"] = estimates[4][0]
        result["daily_review_read_cost"] = estimates[4][0]
        result["read_cost_samples"] = {
            kind: item[1] for kind, item in zip(KINDS, estimates, strict=True)
        }
        measured = sum(estimates[i][2] for i in (0, 4))
        result["read_cost_source"] = (
            "measured" if measured == 2 else "mixed" if measured else "conservative_estimate"
        )
    except Exception:
        pass
    return result
