"""Bounded optional evidence writes outside the Telegram receiver thread.

Queue acceptance is not protected-slot admission. Only a successful store write
can admit a slot; overflow retains the existing business gap cue and every prior
protection promise. No code here changes an SDK protocol cursor.
"""

from __future__ import annotations

import asyncio
import json
import hashlib
import time
from collections import Counter, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4
from pathlib import Path

from telethon import utils
from telethon.extensions import BinaryReader
from telethon.tl import types

MAX_ITEMS = 128
MAX_BYTES = 8 * 1024 * 1024
MAX_ITEM_BYTES = 256 * 1024
EVENT_REUSE_SECONDS = 90
MAINTENANCE_SECONDS = 300
_worker: EvidenceIngestWorker | None = None
_startup_lock: asyncio.Lock | None = None
_startup_loop: asyncio.AbstractEventLoop | None = None
try:
    _BOOT_EPOCH = hashlib.sha256(Path("/proc/sys/kernel/random/boot_id").read_bytes()).hexdigest()[:32]
except OSError:
    _BOOT_EPOCH = uuid4().hex  # Windows tests; production Linux shares a host boot fence.


@dataclass(frozen=True, slots=True)
class _Pending:
    account_id: int
    envelope: bytes
    policy: bytes

    @property
    def size(self) -> int:
        return len(self.envelope) + len(self.policy)


class _CheckedStore:
    """Turn swallowed optional-store failures into a local reuse fence."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.failed = False

    def __getattr__(self, name: str) -> Any:
        value = getattr(self.store, name)
        if not name.endswith("_sync") or not callable(value):
            return value

        def checked(*args: Any, **kwargs: Any) -> Any:
            result = value(*args, **kwargs)
            # Rejected new cache/admission work creates no protection promise.
            # Control/list failures can hide changes to an existing promise.
            if name not in {"capture_sync", "admit_observation_sync"}:
                self.failed = self.failed or getattr(result, "ok", True) is False
            return result

        return checked


class EvidenceIngestWorker:
    def __init__(self, store: Any, *, max_items: int = MAX_ITEMS,
                 max_bytes: int = MAX_BYTES, max_item_bytes: int = MAX_ITEM_BYTES) -> None:
        self.store = store
        self.max_items, self.max_bytes = max_items, max_bytes
        self.max_item_bytes = max_item_bytes
        self.pending: deque[_Pending] = deque()
        self.pending_bytes = 0
        self.inflight_bytes = 0
        self.inflight = False
        self.account_pending: Counter[int] = Counter()
        self.protected: dict[int, dict[int, dict]] = {}
        self.dirty_until = 0.0
        self.controls_pending = False
        self.gap_token = uuid4().hex
        self.gap_generation = 0
        self.gap_order = time.monotonic_ns()
        self.gap_source = "other"
        try:
            from app.core.config import settings
            role = settings.REVIEW_READ_METRICS_ROLE
            if role in {"backend", "growth", "celery"}:
                self.gap_source = role
        except (ImportError, AttributeError):
            pass
        self.accepting = False
        self.wake = asyncio.Event()
        self.idle = asyncio.Event()
        self.idle.set()
        self.task: asyncio.Task | None = None
        self.maintenance_task: asyncio.Task | None = None
        self.rejected = 0
        self.last_error: str | None = None

    async def start(self) -> bool:
        # Allocation/fsync happens before receivers can submit their first item.
        initialized = await self.store.initialize()
        if not initialized.ok:
            self.last_error = initialized.reason
            return False
        self.controls_pending = True
        if not await asyncio.to_thread(self._record_gap_sync):
            return False
        self.controls_pending = False
        self.accepting = True
        self.task = asyncio.create_task(self._run(), name="review-evidence-ingest")
        self.maintenance_task = asyncio.create_task(self._maintain(), name="review-evidence-maintenance")
        return True

    def safe(self, account_id: int) -> bool:
        return bool(self.accepting and not self.controls_pending
                    and not self.account_pending.get(account_id)
                    and time.monotonic() >= self.dirty_until)

    def _dirty(self, reason: str) -> None:
        # A new real failure is distinct from retries needed to fence pending
        # candidates. It must invalidate any handoff prepared after the last one.
        self.gap_token = uuid4().hex
        self.gap_generation += 1
        self.gap_order = max(self.gap_order + 1, time.monotonic_ns())
        self.dirty_until = max(self.dirty_until, time.monotonic() + EVENT_REUSE_SECONDS)
        self.controls_pending = True
        self.last_error = reason
        self.idle.clear()
        self.wake.set()

    def _record_gap_sync(self) -> bool:
        result = self.store.record_ingest_gap_sync(self.gap_source, self.gap_token,
                                                 epoch=(_BOOT_EPOCH, self.gap_order))
        if not result.ok:
            return False
        # A restart/overflow cannot leave a recent ordinary push reusable after
        # its durable gap cue has already been consumed by the business worker.
        self.protected = self._protected_scope((result.data or {}).get("observations", []))
        return True

    @staticmethod
    def _protected_scope(rows: list[dict]) -> dict[int, dict[int, dict]]:
        protected: dict[int, dict[int, dict]] = {}
        for row in rows:
            control = row["control"]
            peer = int(control["group_id"])
            member = protected.setdefault(control["account_id"], {}).setdefault(peer, {
                "joined_at": datetime.fromtimestamp(control["joined_at"], UTC).replace(tzinfo=None).isoformat(),
                "membership_id": control.get("metadata", {}).get("membership_id"),
                "watched_evidence": [], "evidence_needed": False,
            })
            if control["message_id"] not in member["watched_evidence"]:
                member["watched_evidence"].append(control["message_id"])
        return protected

    def submit(self, envelope: Any, policy: dict | None) -> tuple[dict, list[dict]]:
        if not policy or type(policy.get("account_id")) is not int:
            return {}, []
        from app.modules.acquisition import review_read_runtime as runtime

        account_id = policy["account_id"]
        allow_admission = runtime.store_for(account_id) is self.store
        scoped, refs = self._scope(envelope, policy, allow_admission)
        if scoped is None:
            return {}, []
        try:
            packed_policy = json.dumps(scoped, separators=(",", ":")).encode()
            payload = bytes(envelope)
            item = _Pending(account_id, payload, packed_policy)
        except (ValueError, TypeError, AttributeError, OverflowError):
            self._dirty("evidence_ingest_encoding_failed")
            return {}, self._gap_facts(scoped)
        if (not self.accepting or item.size > self.max_item_bytes
                or len(self.pending) + int(self.inflight) >= self.max_items
                or self.pending_bytes + self.inflight_bytes + item.size > self.max_bytes):
            self.rejected += 1
            self._dirty("evidence_ingest_queue_full")
            return {}, self._gap_facts(scoped)
        self.pending.append(item)
        self.pending_bytes += item.size
        self.account_pending[account_id] += 1
        self.idle.clear()
        self.wake.set()
        return refs if allow_admission else {}, []

    def _scope(self, envelope: Any, policy: dict, allow_admission: bool) -> tuple[dict | None, dict]:
        from app.core.account.listener_protocol import updates_in
        from app.modules.acquisition.group_qualification import (
            WARNING,
            looks_like_ad,
            possible_rule,
        )
        from app.modules.acquisition.review_read_runtime import content_version

        account_id = policy["account_id"]
        known = self.protected.get(account_id, {})
        peers: set[int] = set()
        messages = []
        interesting = False
        for update in updates_in(envelope):
            message = getattr(update, "message", None)
            if isinstance(envelope, types.UpdateShortChatMessage):
                message = types.Message(id=envelope.id, peer_id=types.PeerChat(envelope.chat_id),
                                        from_id=types.PeerUser(envelope.from_id), date=envelope.date,
                                        message=envelope.message, out=envelope.out,
                                        reply_to=envelope.reply_to)
            if isinstance(message, types.Message):
                peer = utils.get_peer_id(message.peer_id)
                member = policy.get("groups", {}).get(peer) or known.get(peer)
                if not member or message.out:
                    continue
                body = message.message or ""
                if not (isinstance(update, (types.UpdateEditMessage, types.UpdateEditChannelMessage))
                        or looks_like_ad(body) or possible_rule(body) or WARNING.search(body)
                        or message.reply_to or message.id in member.get("watched_evidence", [])):
                    continue
                peers.add(peer)
                messages.append((peer, message, member))
                interesting = True
            elif isinstance(update, (types.UpdateDeleteChannelMessages, types.UpdateChannel,
                                     types.UpdateChannelTooLong, types.UpdateChannelParticipant)):
                peers.add(-(10**12 + update.channel_id))
                interesting = True
            elif isinstance(update, (types.UpdateDeleteMessages, types.UpdatesTooLong)):
                peers.update(known)
                interesting = True
        if not interesting:
            return None, {}
        groups, refs = {}, {}
        for peer in peers:
            member = policy.get("groups", {}).get(peer) or known.get(peer)
            if member:
                groups[peer] = {
                    "joined_at": member.get("joined_at"),
                    "membership_id": member.get("membership_id"),
                    "evidence_needed": bool(member.get("evidence_needed")),
                    "watched_evidence": list(member.get("watched_evidence", []))[:512],
                }
        for peer, message, member in messages:
            if not allow_admission:
                continue
            # TL serializes dates at second precision. Compute the reference
            # for the exact frozen wire version the writer will deserialize.
            version = content_version(message.message or "", sender_id=message.sender_id,
                                      date=message.date.replace(microsecond=0) if message.date else None,
                                      edit_date=message.edit_date.replace(microsecond=0) if message.edit_date else None)
            refs[peer, message.id] = {"version": version, "account_id": account_id,
                                      "group_id": peer, "joined_at": member.get("joined_at"),
                                      "message_id": message.id, "key": f"message:{message.id}:{version}"}
        return {"account_id": account_id, "loaded_at": policy.get("loaded_at", 0),
                "selective": policy.get("selective", False), "groups": groups}, refs

    @staticmethod
    def _gap_facts(policy: dict) -> list[dict]:
        return [{"key": f"gap:{peer}:evidence_ingest", "kind": "gap", "peer": int(peer),
                 "at": time.time(), "joined_at": member.get("joined_at"),
                 "reason": "optional_evidence_ingest_gap"}
                for peer, member in policy.get("groups", {}).items()]

    def _capture_sync(self, item: _Pending) -> bool:
        from app.modules.acquisition import review_read_runtime as runtime

        with BinaryReader(item.envelope) as reader:
            envelope = reader.tgread_object()
        policy = json.loads(item.policy)
        policy["groups"] = {int(peer): member for peer, member in policy["groups"].items()}
        # Other workers can admit slots too; expand the control scope from the
        # actual fixed ledger without retaining a second payload copy.
        current = self.store.list_observations_sync(account_id=item.account_id)
        if not current.ok:
            return False
        live_scope = self._protected_scope((current.data or {}).get("observations", []))
        policy["groups"] = {**live_scope.get(item.account_id, {}), **policy["groups"]}
        checked = _CheckedStore(self.store)
        runtime.capture_envelope(envelope, policy, store=checked,
                                 allow_admission=runtime.store_for(item.account_id) is self.store)
        # Refresh the bounded (at most protected-slot-count) control-only scope.
        result = self.store.list_observations_sync()
        if result.ok:
            self.protected = self._protected_scope((result.data or {}).get("observations", []))
        return result.ok and not checked.failed

    async def _run(self) -> None:
        while self.accepting or self.pending or self.controls_pending:
            if self.controls_pending:
                generation = self.gap_generation
                succeeded = await asyncio.to_thread(self._record_gap_sync)
                # A receiver can report a new failure while the preceding batch
                # is on disk. Finishing that older batch must not clear its cue.
                self.controls_pending = not succeeded or generation != self.gap_generation
                if not succeeded:
                    self.last_error = "evidence_ingest_control_write_failed"
                    if not self.accepting:
                        break  # Keep all protected payloads for startup recovery.
                    await asyncio.sleep(1)
                    continue
                if self.controls_pending:
                    continue  # Finish a newer failure before clearing the wake/idle signals.
            if not self.pending:
                self.idle.set()
                self.wake.clear()
                if not self.accepting:
                    break
                await self.wake.wait()
                continue
            item = self.pending.popleft()
            self.pending_bytes -= item.size
            self.inflight, self.inflight_bytes = True, item.size
            try:
                if not await asyncio.to_thread(self._capture_sync, item):
                    self._dirty("evidence_ingest_store_write_failed")
            except Exception as exc:
                self._dirty(type(exc).__name__)
            finally:
                self.account_pending[item.account_id] -= 1
                if not self.account_pending[item.account_id]:
                    self.account_pending.pop(item.account_id)
                self.inflight, self.inflight_bytes = False, 0
                if time.monotonic() < self.dirty_until:
                    # A candidate queued before an overflow may only become a
                    # protected slot now. It must inherit that intervening gap.
                    self.controls_pending = True
        self.idle.set()

    async def _maintain(self) -> None:
        from app.modules.acquisition.review_evidence_handoff import maintain_evidence_store

        while True:
            await asyncio.sleep(MAINTENANCE_SECONDS)
            try:
                await maintain_evidence_store(account_id=None)
            except Exception as exc:
                self.last_error = type(exc).__name__

    async def drain(self) -> None:
        await self.idle.wait()

    async def stop(self) -> None:
        self.accepting = False
        self.wake.set()
        if self.maintenance_task is not None:
            self.maintenance_task.cancel()
            try:
                await self.maintenance_task
            except asyncio.CancelledError:
                pass
        if self.task is not None:
            await self.task


def _lifecycle_lock() -> asyncio.Lock:
    global _startup_lock, _startup_loop
    loop = asyncio.get_running_loop()
    if _startup_lock is None or _startup_loop is not loop:
        _startup_loop, _startup_lock = loop, asyncio.Lock()
    return _startup_lock


async def start_ingest_worker(*, store: Any = None) -> EvidenceIngestWorker | None:
    async with _lifecycle_lock():
        worker = await _start(store)
        if worker is not None:
            from app.modules.acquisition.review_evidence_handoff import resume_owner_maintenance
            resume_owner_maintenance()
        return worker


async def _start(store: Any) -> EvidenceIngestWorker | None:
    global _worker
    if _worker is not None:
        return _worker
    if store is None:
        from app.core.config import settings
        from app.modules.acquisition import review_read_runtime as runtime

        store = await asyncio.to_thread(runtime.maintenance_store)
        if store is None:
            for value in settings.REVIEW_READ_OPTIMIZATION_ACCOUNT_IDS.split(","):
                if value.strip().isdigit():
                    store = runtime.store_for(int(value.strip()))
                    if store is not None:
                        break
    if store is None:
        return None
    worker = EvidenceIngestWorker(store)
    if not await worker.start():
        return None
    _worker = worker
    return worker


def submit_envelope(envelope: Any, policy: dict | None) -> tuple[dict, list[dict]]:
    return _worker.submit(envelope, policy) if _worker is not None else ({}, [])


def cache_safe(account_id: int) -> bool:
    return _worker.safe(account_id) if _worker is not None else False


def cache_state(account_id: int) -> dict[str, Any]:
    """Report bounded control state, without copying messages or reading Telegram."""
    worker = _worker
    if worker is None:
        return {"safe": False, "reason": "ingest_unavailable"}
    reason = ("ingest_stopped" if not worker.accepting else
              "ingest_controls_pending" if worker.controls_pending else
              "ingest_account_pending" if worker.account_pending.get(account_id) else
              "ingest_dirty" if time.monotonic() < worker.dirty_until else "ready")
    return {"safe": worker.safe(account_id), "reason": reason,
            "pending_items": min(worker.max_items, len(worker.pending) + int(worker.inflight)),
            "account_pending": min(worker.max_items, worker.account_pending.get(account_id, 0)),
            "controls_pending": bool(worker.controls_pending),
            "dirty_seconds": min(EVENT_REUSE_SECONDS, max(0, round(worker.dirty_until-time.monotonic()))),
            "rejected": min((1 << 63)-1, worker.rejected)}


async def drain_ingest() -> None:
    if _worker is not None:
        await _worker.drain()


async def stop_ingest_worker() -> None:
    global _worker
    async with _lifecycle_lock():
        from app.modules.acquisition.review_evidence_handoff import stop_owner_maintenance
        await stop_owner_maintenance()
        worker = _worker
        if worker is not None:
            try:
                await worker.stop()
            finally:
                _worker = None
