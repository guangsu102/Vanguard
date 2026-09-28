"""Keep unrelated channel pushes flowing during a pre-RPC budget wait.

Channel gaps allow unrelated push processing. Global gaps keep their request
and seq frozen while incoming raw envelopes are durably queued. No fake RPC
result or cursor advancement is used here.
"""

from __future__ import annotations

import asyncio
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
    # Preserve the exact pending request and SDK pts/seq. Incoming envelopes are
    # durably queued while waiting; only the SDK can order them after the reply.
    await asyncio.sleep(min(30, deferred.retry_after_seconds))
