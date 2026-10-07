"""Keep unrelated channel pushes flowing during a pre-RPC budget wait.

Channel gaps allow unrelated push processing. Account gaps keep their request,
pts/seq/date frozen while independent channel pushes continue through SDK
ordering. Account/private input remains durable until real recovery completes.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta
from typing import Any

from telethon import __version__ as telethon_version
from telethon._updates import GapError
from telethon._updates.messagebox import MessageBox
from telethon.tl.functions.updates import GetChannelDifferenceRequest, GetDifferenceRequest


def can_receive_while_waiting(client: Any, requests: list[Any]) -> bool:
    return bool(
        telethon_version in {"1.44.0", "1.45.0"}
        and len(requests) == 1
        and isinstance(requests[0], GetChannelDifferenceRequest)
        and getattr(client, "_vanguard_durable_checkpoint", False)
        and getattr(client, "_sequential_updates", False)
        and isinstance(getattr(client, "_message_box", None), MessageBox)
    )


async def receive_while_waiting(client: Any, deferred: Any) -> None:
    previous = getattr(client, "_vanguard_sync_wait", None) or {}
    client._vanguard_sync_wait = {
        "started": previous.get("started", time.monotonic()),
        "reason": deferred.reason,
        "resume_at": (
            datetime.utcnow() + timedelta(seconds=deferred.retry_after_seconds)
        ).isoformat(),
    }
    # Recheck actual Telegram cooldowns and all shared budgets at least every 30s.
    deadline = asyncio.get_running_loop().time() + min(30, deferred.retry_after_seconds)
    while client.is_connected():
        client._message_box.check_deadlines()
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return
        try:
            envelope = await asyncio.wait_for(client._updates_queue.get(), remaining)
        except TimeoutError:
            return
        processed = []
        try:
            users, chats = client._message_box.process_updates(
                envelope, client._mb_entity_cache, processed
            )
        except GapError:
            # The SDK retains the original pts/seq and the required difference.
            continue
        ordered = await client._preprocess_updates(processed, users, chats)
        for update in ordered:
            await client._dispatch_update(update)


async def record_sync_result(governor: Any, requests: list[Any], result: Any) -> None:
    """Bounded aggregate counters only; observability never changes RPC outcomes."""
    from app.core.redis import get_redis

    try:
        results = result if isinstance(result, (list, tuple)) else [result]
        now = datetime.utcnow()
        cache = await get_redis()
        async with cache.pipeline(transaction=True) as pipe:
            key = f"vanguard:rpc:sync_results:{governor.account_id}:{now:%Y%m%d}"
            for request, value in zip(requests, results, strict=True):
                scope = "channel" if isinstance(request, GetChannelDifferenceRequest) else "account"
                kind = type(value).__name__
                if scope == "account":
                    state = getattr(value, "intermediate_state", None) or getattr(value, "state", None)
                    progress = {
                        "at": now.isoformat(), "result": kind,
                        "request_pts": getattr(request, "pts", None),
                        "request_date": str(getattr(request, "date", None)),
                        "result_pts": getattr(state, "pts", None),
                        "result_date": str(getattr(state, "date", None)),
                        "result_seq": getattr(state, "seq", None),
                        "updates": len(getattr(value, "other_updates", None) or []),
                    }
                    pipe.set(f"vanguard:rpc:sync_progress:{governor.account_id}",
                             json.dumps(progress), ex=86400)
                if kind not in {
                    "ChannelDifference",
                    "ChannelDifferenceEmpty",
                    "ChannelDifferenceTooLong",
                    "Difference",
                    "DifferenceEmpty",
                    "DifferenceSlice",
                    "DifferenceTooLong",
                    "State",
                }:
                    kind = "other"
                pipe.hincrby(key, f"{scope}|{kind}|responses", 1)
                for field in ("new_messages", "other_updates"):
                    pipe.hincrby(
                        key, f"{scope}|{kind}|{field}", len(getattr(value, field, None) or [])
                    )
                # Channel messages also arrive inside other_updates. Counting
                # only new_messages incorrectly makes useful catch-up look empty.
                counts: dict[str, int] = {}
                for update in getattr(value, "other_updates", None) or []:
                    label = type(update).__name__
                    if label not in {"UpdateNewChannelMessage", "UpdateNewMessage",
                                     "UpdateEditChannelMessage", "UpdateEditMessage",
                                     "UpdateDeleteChannelMessages", "UpdateDeleteMessages",
                                     "UpdateChannelTooLong"}:
                        label = "other"
                    counts[label] = counts.get(label, 0) + 1
                for label, count in counts.items():
                    pipe.hincrby(key, f"{scope}|{kind}|update_type|{label}", count)
            pipe.expire(key, 7 * 86400)
            await pipe.execute()
    except Exception:
        # These measurements must not turn a received difference into a retry.
        return


def can_wait_global(client: Any, requests: list[Any]) -> bool:
    from app.core.account.listener_journal import JournalQueue

    return bool(
        telethon_version in {"1.44.0", "1.45.0"}
        and len(requests) == 1
        and isinstance(requests[0], GetDifferenceRequest)
        and getattr(client, "_vanguard_durable_checkpoint", False)
        and getattr(client, "_sequential_updates", False)
        and isinstance(getattr(client, "_updates_queue", None), JournalQueue)
    )


async def wait_global(client: Any, deferred: Any) -> None:
    previous = getattr(client, "_vanguard_sync_wait", None) or {}
    client._vanguard_sync_wait = {
        "started": previous.get("started", time.monotonic()),
        "scope": "account",
        "reason": deferred.reason,
        "resume_at": (
            datetime.utcnow() + timedelta(seconds=deferred.retry_after_seconds)
        ).isoformat(),
    }
    from app.core.account.listener_journal import JournalQueue

    queue = getattr(client, "_updates_queue", None)
    if not isinstance(queue, JournalQueue):
        await asyncio.sleep(min(30, deferred.retry_after_seconds))
        return
    deadline = asyncio.get_running_loop().time() + min(30, deferred.retry_after_seconds)
    queue.park_global_protocol()
    try:
        while client.is_connected():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            try:
                envelope = queue.get_channel_nowait()
            except asyncio.QueueEmpty:
                await asyncio.sleep(min(0.25, remaining))
                continue
            box = client._message_box
            date, seq = box.date, box.seq
            # SDK process_updates also revisits possible gaps and updates the
            # account date. Isolate channel gaps, then restore global state
            # before any await/checkpoint. No global/private input is consumed.
            global_gaps = {key: value for key, value in box.possible_gaps.items()
                           if type(key) is not int}
            box.possible_gaps = {key: value for key, value in box.possible_gaps.items()
                                 if type(key) is int}
            processed = []
            try:
                users, chats = box.process_updates(envelope, client._mb_entity_cache, processed)
            finally:
                box.date, box.seq = date, seq
                box.possible_gaps.update(global_gaps)
            ordered = await client._preprocess_updates(processed, users, chats)
            for update in ordered:
                await client._dispatch_update(update)
            await asyncio.sleep(0)
    finally:
        queue.hold_global_protocol = False
        queue._fill_protocol_recovery()
