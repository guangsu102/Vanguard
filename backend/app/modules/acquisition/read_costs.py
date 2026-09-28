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

KINDS = ("delivery", "two_minute", "one_hour", "twenty_four_hour")
MIN_SAMPLES = 20
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
            name, json.dumps({"at": time.time(), "reads": meter.reads, "complete": meter.complete})
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


def estimate(samples: list[Any], fallback: int, now: float) -> tuple[int, int, bool]:
    valid = []
    for raw in samples:
        try:
            row = json.loads(raw)
            if (
                type(row["reads"]) is int
                and 0 < row["reads"] <= 10000
                and type(row["complete"]) is bool
                and now - RETENTION <= float(row["at"]) <= now
            ):
                valid.append(row)
        except (ValueError, TypeError, KeyError):
            continue
    successes = sorted(row["reads"] for row in valid if row["complete"])
    if len(successes) < MIN_SAMPLES:
        return fallback, len(successes), False
    # Include the reads spent by unsuccessful attempts in the per-success cost.
    cost = max(
        successes[math.ceil(len(successes) * 0.9) - 1],
        math.ceil(sum(row["reads"] for row in valid) / len(successes)),
    )
    return max(1, cost), len(successes), True


async def operation_costs(account_id: int) -> dict[str, Any]:
    from app.core.redis import get_redis

    result = {
        "delivery_read_cost": 12,
        "survival_read_cost": 30,
        "read_cost_source": "conservative_estimate",
        "read_cost_samples": {},
    }
    try:
        cache = await get_redis()
        async with asyncio.timeout(1):
            rows = await asyncio.gather(
                *(cache.lrange(key(account_id, kind), 0, MAX_SAMPLES - 1) for kind in KINDS)
            )
        estimates = [
            estimate(samples, 12 if kind == "delivery" else 10, time.time())
            for kind, samples in zip(KINDS, rows, strict=True)
        ]
        result["delivery_read_cost"] = estimates[0][0]
        result["survival_read_cost"] = sum(item[0] for item in estimates[1:])
        result["read_cost_samples"] = {
            kind: item[1] for kind, item in zip(KINDS, estimates, strict=True)
        }
        measured = sum(item[2] for item in estimates)
        result["read_cost_source"] = (
            "measured" if measured == 4 else "mixed" if measured else "conservative_estimate"
        )
    except Exception:
        pass
    return result
