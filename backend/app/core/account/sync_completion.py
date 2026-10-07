"""Spend bounded idle survival capacity to finish a progressing difference."""

from datetime import UTC, datetime
from typing import Any


def cursor(request: Any) -> tuple | None:
    from telethon.tl.functions.updates import GetChannelDifferenceRequest, GetDifferenceRequest

    if isinstance(request, GetDifferenceRequest):
        date = request.date
        if isinstance(date, datetime):
            date = (date.replace(tzinfo=UTC) if date.tzinfo is None else date).timestamp()
        return ("account", 0, request.pts, request.qts, int(date or 0))
    if isinstance(request, GetChannelDifferenceRequest):
        return ("channel", request.channel.channel_id, request.pts)
    return None


def next_cursor(request: Any, result: Any) -> tuple | None:
    """Only a real unfinished response with monotonic progress grants access."""
    before = cursor(request)
    if before is None:
        return None
    if before[0] == "account" and type(result).__name__ == "DifferenceSlice":
        state = result.intermediate_state
        date = state.date
        if isinstance(date, datetime):
            date = (date.replace(tzinfo=UTC) if date.tzinfo is None else date).timestamp()
        after = ("account", 0, state.pts, state.qts, int(date or 0))
    elif (before[0] == "channel" and type(result).__name__ == "ChannelDifference"
          and result.final is False):
        after = (*before[:2], result.pts)
    else:
        return None
    return after if after != before and all(b >= a for a, b in zip(before[2:], after[2:], strict=True)) else None


def continuing(client: Any, requests: list[Any]) -> bool:
    if not getattr(client, "_vanguard_durable_checkpoint", False) or len(requests) != 1:
        return False
    point = cursor(requests[0])
    return point is not None and (getattr(client, "_vanguard_sync_continuations", {}) or {}).get(point[:2]) == point


def record(client: Any, requests: list[Any], result: Any) -> None:
    if len(requests) != 1 or cursor(requests[0]) is None:
        return
    points = dict(getattr(client, "_vanguard_sync_continuations", {}) or {})
    point = cursor(requests[0])
    points.pop(point[:2], None)
    following = next_cursor(requests[0], result)
    if following is not None:
        points[following[:2]] = following
    while len(points) > 64:
        points.pop(next(iter(points)))
    client._vanguard_sync_continuations = points


async def reserve(cache: Any, account_id: int, snapshot: dict, reads: int) -> int:
    """No loan from ads/reviews; every granted read stays in the same totals."""
    from app.core.account.reserved_reads import RESERVED_BUDGET_LUA, reservation_args

    limits = snapshot["limits"]
    if snapshot["state"] in {"cooldown", "unavailable"}:
        return max(1, snapshot.get("retry_after_seconds", 60)) * 1000
    if any(limits.get("survival_hold_" + w, -1) < 0 for w in ("hour", "day")):
        return 60000  # Active/uncertain survival readers keep their reservation.
    keys, args = reservation_args(account_id, limits, "survival", reads, 0, sync_completion=True)
    return int(await cache.eval(RESERVED_BUDGET_LUA, len(keys), *keys, *args))
