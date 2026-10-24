"""Typed, bounded Telegram reads executed by the session-owning Actor.

Workers submit serializable requests; only an active ``session_exec`` stage
can acquire the connected client.  Reads are collected for one second in each
submitting process, at most 50 per owner quantum, and remain serial on the
owner side. The existing RPC governor remains the quota authority.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, unquote
from weakref import WeakKeyDictionary

from app.core.account import session_exec
from app.core.redis import get_redis

STAGE_PREFIX = "p3_read:"
BATCH_WINDOW_SECONDS = 1.0
MAX_BATCH_SIZE = 50
READ_KINDS = frozenset({"entity", "membership", "permissions", "survival"})
_batches: WeakKeyDictionary = WeakKeyDictionary()


def wire_value(value: Any) -> Any:
    """Keep facts and InputPeer identity serializable across the queue."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): wire_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [wire_value(item) for item in value]
    attrs = {"_type": type(value).__name__}
    for name in (
        "id", "access_hash", "username", "title", "first_name", "last_name",
        "megagroup", "gigagroup", "broadcast", "has_left", "left", "deleted",
        "is_banned", "is_member", "is_admin", "is_creator", "send_messages",
        "send_media", "date", "edit_date", "message", "post", "out",
    ):
        if hasattr(value, name):
            item = getattr(value, name)
            attrs[name] = item.isoformat() if hasattr(item, "isoformat") else wire_value(item)
    try:
        from telethon.utils import get_peer_id

        attrs["peer_id"] = get_peer_id(value)
    except (TypeError, ValueError):
        pass
    return attrs


def _request(payload: Mapping[str, Any]) -> dict[str, Any]:
    kind = str(payload.get("kind") or "")
    target = str(payload.get("target_key") or "").strip()
    if kind not in READ_KINDS or not target or len(target) > 512:
        raise ValueError("telegram_read_request_invalid")
    message = payload.get("message_key")
    if kind == "survival" and (message is None or int(message) <= 0):
        raise ValueError("telegram_survival_message_required")
    return {
        "kind": kind,
        "account_id": int(payload["account_id"]),
        "target_key": target,
        "message_key": str(message) if message is not None else None,
        "force_refresh": bool(payload.get("force_refresh")),
        "local_fact_version": payload.get("local_fact_version"),
    }


def _unknown(reason: str) -> dict[str, Any]:
    return {"status": "unknown", "data": None, "error": reason}


async def _fact_version(target: str) -> str:
    from sqlalchemy import select

    from app.core.database import get_db_session
    from app.modules.acquisition.qualification_snapshot import QualificationFactVersion

    async with get_db_session() as db:
        value = await db.scalar(
            select(QualificationFactVersion.version).where(
                QualificationFactVersion.target_key == target
            )
        )
    return str(value or 0)


async def _assert_generation(wrapper: Any, expected: str | None = None) -> str:
    guard = getattr(wrapper, "session_guard", None)
    lease = getattr(guard, "lease", None)
    token = getattr(lease, "token", None)
    if not token or (expected is not None and token != expected):
        raise RuntimeError("telegram_actor_generation_mismatch")
    await guard.assert_owned(expected_token=expected or token)
    return str(token)


async def execute_read(account_pool: Any, request: Mapping[str, Any], *,
                       expected_generation: str | None = None) -> dict[str, Any]:
    """Owner-only provider boundary; no worker fallback is permitted."""
    request = _request(request)
    account_id = request["account_id"]
    if session_exec.current_actor_account() != account_id:
        raise RuntimeError("telegram_read_requires_actor_context")
    if account_pool is None or not account_pool.peek_connected(account_id):
        raise RuntimeError("telegram_actor_not_connected")
    target = request["target_key"]
    before_version = await _fact_version(target)
    required = request.get("local_fact_version")
    if required is not None and int(before_version) < int(required):
        return _unknown("telegram_fact_version_unavailable")
    wrapper = await account_pool.acquire_by_id(
        account_id, purpose=f"p3_read:{request['kind']}",
        allow_restricted=True, raise_on_lease_failure=True,
    )
    if wrapper is None:
        raise RuntimeError("telegram_actor_unavailable")
    try:
        generation = await _assert_generation(wrapper, expected_generation)
        client = wrapper.get_client() if hasattr(wrapper, "get_client") else wrapper.client
        if client is None:
            raise RuntimeError("telegram_actor_client_unavailable")
        # Numeric Telegram IDs must remain integers; passing "-100..." as a
        # string makes Telethon attempt an unrelated username lookup.
        provider_target: Any = int(target) if target.lstrip("-").isdigit() else target
        entity = await client.get_entity(provider_target)
        kind = request["kind"]
        if kind == "entity":
            value = entity
        elif kind in {"membership", "permissions"}:
            value = await client.get_permissions(entity, "me")
        else:
            value = await client.get_messages(entity, ids=int(request["message_key"]))
        await _assert_generation(wrapper, generation)
        if session_exec.current_actor_account() != account_id:
            return _unknown("telegram_actor_context_expired")
        after_version = await _fact_version(target)
        if before_version != after_version:
            return _unknown("telegram_fact_changed_during_read")
        data = wire_value(value)
        from app.modules.acquisition.qualification_snapshot import derive_evidence_hash

        return {
            "status": "fresh", "data": data, "source_version": after_version,
            "evidence_hash": derive_evidence_hash({"kind": kind, "data": data}),
            "actor_generation": generation, "fetched_at": time.time(), "source": "actor",
        }
    finally:
        await account_pool.release(wrapper)


async def execute_owner_batch(account_pool: Any, account_id: int, stage: str) -> tuple[dict, int]:
    """Handle one queue item under session_exec's account lock."""
    if not stage.startswith(STAGE_PREFIX) or len(stage) > 128_000:
        raise ValueError("telegram_read_batch_invalid")
    envelope = json.loads(unquote(stage[len(STAGE_PREFIX):]))
    requests = envelope.get("reads")
    generation = envelope.get("actor_generation")
    if not isinstance(requests, list) or not 1 <= len(requests) <= MAX_BATCH_SIZE or not generation:
        raise ValueError("telegram_read_batch_invalid")
    results = []
    for payload in requests:
        request = _request(payload)
        if request["account_id"] != account_id:
            raise ValueError("telegram_read_account_mismatch")
        try:
            result = await execute_read(account_pool, request, expected_generation=generation)
        except Exception as exc:
            result = _unknown(f"{type(exc).__name__}:{exc}")
            result["wait_seconds"] = int(getattr(exc, "seconds", 0) or 0)
            result["error_type"] = type(exc).__name__
        results.append(result)
    return {"reads": results, "actor_generation": generation}, 1


async def _flush(account_id: int, items: list, batches: dict) -> None:
    await asyncio.sleep(BATCH_WINDOW_SECONDS)
    if batches.get(account_id) is items:
        batches.pop(account_id, None)
    try:
        cache = await get_redis()
        owner = await session_exec.owner_id(cache, account_id)
        generation = await cache.get(f"vanguard:telegram:lease:{account_id}")
        if isinstance(generation, bytes):
            generation = generation.decode()
        if owner is None or not generation:
            raise RuntimeError("telegram_actor_owner_unavailable")
        stage = STAGE_PREFIX + quote(json.dumps(
            {"reads": [request for request, _ in items], "actor_generation": generation},
            separators=(",", ":"),
        ), safe="")
        reply = await session_exec.forward(account_id, stage)
        if not reply or reply.get("error"):
            raise RuntimeError("telegram_actor_owner_unreachable")
        result = reply.get("result") or {}
        values = result.get("reads")
        if result.get("actor_generation") != generation or not isinstance(values, list) or len(values) != len(items):
            raise RuntimeError("telegram_actor_reply_invalid")
        current = await cache.get(f"vanguard:telegram:lease:{account_id}")
        if isinstance(current, bytes):
            current = current.decode()
        if current != generation:
            raise RuntimeError("telegram_actor_generation_expired")
        for (_, future), value in zip(items, values, strict=True):
            if not future.done():
                future.set_result(value)
    except BaseException as exc:
        for _, future in items:
            if not future.done():
                if isinstance(exc, asyncio.CancelledError):
                    future.cancel()
                else:
                    future.set_exception(exc)


async def submit_read(account_pool: Any, **payload: Any) -> dict[str, Any]:
    request = _request(payload)
    account_id = request["account_id"]
    if session_exec.current_actor_account() == account_id:
        # Reconciliation already runs inside this same queue. Re-enqueueing
        # itself would deadlock on its own account lock.
        return await execute_read(account_pool, request)
    loop = asyncio.get_running_loop()
    batches = _batches.setdefault(loop, {})
    items = batches.get(account_id)
    if items is None or len(items) >= MAX_BATCH_SIZE:
        items = []
        batches[account_id] = items
        asyncio.create_task(_flush(account_id, items, batches))
    future = loop.create_future()
    items.append((request, future))
    return await asyncio.shield(future)
