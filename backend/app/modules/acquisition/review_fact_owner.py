"""Fenced, bounded handoff of raw exact-ID material between review tasks.

The existing AccountPool operation lease remains the sole distributed owner.
Every consumer gets a fresh read context: permissions, participant roles,
full-info and warning queries are never inherited. Only a strictly scoped raw
message bundle may cross a task boundary; it is not authorization or coverage.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import time
import uuid
from typing import Any

from app.modules.acquisition.qualification_read_context import QualificationReadContext, _entity_key

REFERENCE = "raw_fact_handoff"
VERSION = 1


class ReviewFactOwner(QualificationReadContext):
    """One task's context, fenced by the caller's already-held account lease."""

    def __init__(self, *, account_id: int, wrapper: Any, store: Any) -> None:
        super().__init__(enabled=True)
        self.account_id, self.wrapper, self.store = account_id, wrapper, store
        self._lease = getattr(wrapper, "operation_lease", None)
        self._client = getattr(wrapper, "client", None)
        self._entity: Any = None
        self._scope: dict | None = None
        self._identity: str | None = None
        self._group_id: int | None = None
        self._inherited: dict[int, Any] = {}
        self._inherited_observed_at: float | None = None
        self.inherited_used_at: float | None = None
        self._reference: dict | None = None
        self._task_observed_at = time.time()
        self.mandatory_live_ids: set[int] = set()

    def owned(self) -> bool:
        """A released, replaced or lost lease cannot lend or publish material."""
        return bool(self._lease is not None
                    and getattr(self._lease, "account_id", None) == self.account_id
                    and getattr(self.wrapper, "operation_lease", None) is self._lease
                    and not getattr(self.wrapper, "operation_lease_lost", True)
                    and getattr(self.wrapper, "client", None) is self._client)

    def identity(self) -> str | None:
        if not self.owned() or self._group_id is None or self._scope is None:
            return None
        from app.modules.acquisition.review_history_progress import fingerprint
        # A just-received edit/delete can precede SDK PTS application. This
        # optimization has its own fence and cannot depend on another flag's
        # tracker being enabled. Only the fully checkpointed local queue lends
        # material; any pending envelope/fact falls back to the current reader.
        journal = getattr(getattr(self._client, "session", None), "journal", None)
        try:
            if (journal is None or not journal.ordered_complete or journal.qsize()
                    or journal.observed or journal.protocol_recovery
                    or journal.hold_global_protocol or journal.failure):
                return None
            # TooLong recovery can remove an observed item while its durable
            # obligation remains unresolved. Inspect only its indexed state,
            # never deserialize or duplicate the pending message body.
            cursor = journal.session._cursor()
            try:
                unresolved = cursor.execute(
                    "SELECT 1 FROM vanguard_raw_updates WHERE state IN "
                    "('pending','reconciliation_required') LIMIT 1"
                ).fetchone()
            finally:
                cursor.close()
            if unresolved is not None:
                return None
            facts = journal.facts(limit=1001)
            if len(facts) >= 1001 or any(str(json.loads(raw).get("peer")) == str(self._group_id)
                                         for _key, raw in facts):
                return None
        except (AttributeError, TypeError, ValueError, OSError, sqlite3.Error):
            return None
        return fingerprint(self._client, self.account_id, self._group_id, self._scope)

    def _drop_inherited(self) -> None:
        self._inherited.clear()
        self._reference = None
        self._inherited_observed_at = None
        # Discard only inherited raw messages. Current-task live facts keep the
        # original context semantics and never become an authorization cache.
        for key, value in list(self._messages.items()):
            if getattr(value, "_vanguard_fact_handoff", False):
                del self._messages[key]

    def _inheritance_current(self) -> bool:
        return bool(self._identity is not None and self.identity() == self._identity
                    and self._inherited_observed_at is not None
                    and 0 <= time.time()-self._inherited_observed_at < 300)

    def cached_message(self, client: Any, entity: Any, message_id: int) -> tuple[bool, Any]:
        present, value = super().cached_message(client, entity, message_id)
        # Base methods acquire their own lock after the handoff seed. Recheck
        # at the actual cache-consumption boundary in case that lock waited.
        if present and getattr(value, "_vanguard_fact_handoff", False) and (
            message_id in self.mandatory_live_ids or not self._inheritance_current()
        ):
            self._messages.pop(self._key(client, entity, message_id), None)
            return False, None
        return present, value

    async def bind(self, entity: Any, scope: dict, previous: dict) -> None:
        from app.modules.acquisition.review_history_progress import _message, group_key

        self._entity, self._group_id = entity, group_key(entity)
        from app.modules.acquisition.review_history_progress import _date
        if (not isinstance(scope, dict) or type(scope.get("membership_id")) is not int
                or scope["membership_id"] <= 0 or _date(scope.get("joined_at")) is None
                or not scope.get("profile") or not scope.get("system_identity")
                or not scope.get("policy")):
            return
        # No mutable event/settings alias may change underneath the fingerprint.
        self._scope = copy.deepcopy(scope)
        self._identity = self.identity()
        ref = previous.get(REFERENCE)
        if (self._identity is None or not isinstance(ref, dict)
                or ref.get("version") != VERSION or ref.get("identity") != self._identity
                or not isinstance(ref.get("key"), str) or len(ref["key"]) != 32):
            return
        try:
            response = await self.store.load_history_page(self.account_id, self._group_id, key=ref["key"])
            if not response.ok or self.identity() != self._identity:
                return
            data, metadata = response.data, response.data["metadata"]
            observed = float(metadata["observed_at"])
            if (metadata.get("fact_handoff_version") != VERSION
                    or metadata.get("identity") != self._identity
                    or not 0 <= time.time()-observed < 300):
                return
            records = data["records"]
            if len(records) > self.max_messages:
                return
            messages = [_message(record) for record in records]
            if (len({message.id for message in messages}) != len(messages)
                    or any(type(message.id) is not int or message.id <= 0
                           or message.date is None or type(message.sender_id) is not int or message.sender_id <= 0
                           or message.action for message in messages)):
                return
            for message in messages:
                message._vanguard_fact_handoff = True
            self._inherited = {message.id: message for message in messages}
            self._inherited_observed_at = observed
            self._reference = dict(ref)
        except (AttributeError, KeyError, ValueError, TypeError, OSError):
            return

    def _seed(self, client: Any, entity: Any, identifiers: list[int], *, require_live: bool) -> None:
        if self.identity() != self._identity or self._identity is None:
            self._drop_inherited()
            return
        if client is not self._client or _entity_key(entity) != _entity_key(self._entity):
            return
        if (self._inherited_observed_at is None
                or not 0 <= time.time()-self._inherited_observed_at < 300):
            self._drop_inherited()
            return
        values = [self._inherited[mid] for mid in identifiers if mid in self._inherited
                  and not require_live and mid not in self.mandatory_live_ids
                  and not self.cached_message(client, entity, mid)[0]]
        if values:
            self.inherited_used_at = min(self.inherited_used_at or self._inherited_observed_at,
                                         self._inherited_observed_at)
        self.remember_messages(client, entity, values)

    async def get_messages(self, client: Any, entity: Any, ids: Any,
                           *, require_live: bool = False) -> list[Any]:
        identifiers = list(dict.fromkeys(ids))
        self._seed(client, entity, identifiers, require_live=require_live)
        # Previously inherited IDs can become mandatory after a proof/rule is
        # selected. Remove them before the ordinary batching layer runs.
        for mid in self.mandatory_live_ids:
            key = self._key(client, entity, mid)
            if getattr(self._messages.get(key), "_vanguard_fact_handoff", False):
                self._messages.pop(key, None)
        values = await super().get_messages(client, entity, identifiers, require_live=require_live)
        # A missing subset may have awaited a current RPC while inherited
        # values were already selected. If a change arrived in that interval,
        # discard only the inherited subset and read it through the same guard.
        if any(getattr(value, "_vanguard_fact_handoff", False) for value in values) and not self._inheritance_current():
            self._drop_inherited()
            values = await super().get_messages(client, entity, identifiers, require_live=True)
        return values

    async def get_message(self, client: Any, entity: Any, message_id: int,
                          *, require_live: bool = False) -> Any:
        self._seed(client, entity, [message_id], require_live=require_live)
        if message_id in self.mandatory_live_ids:
            require_live = True
        value = await super().get_message(client, entity, message_id, require_live=require_live)
        if getattr(value, "_vanguard_fact_handoff", False) and not self._inheritance_current():
            self._drop_inherited()
            value = await super().get_message(client, entity, message_id, require_live=True)
        return value

    async def publish(self) -> dict | None:
        """Publish raw material only; the reference never changes its age."""
        if self._identity is None or self.identity() != self._identity:
            return None
        from app.modules.acquisition.review_history_progress import _record

        prefix = self._key(self._client, self._entity, 1)[:-1]
        messages = {value.id: value for key, value in self._messages.items()
                    if key[:-1] == prefix and value is not None
                    and type(getattr(value, "id", None)) is int and value.id > 0
                    and getattr(value, "date", None) is not None
                    and type(getattr(value, "sender_id", None)) is int and value.sender_id > 0
                    and not getattr(value, "action", None)
                    and not getattr(value, "_vanguard_event_evidence", False)
                    and not (getattr(value, "_vanguard_history_evidence", False)
                             and not getattr(value, "_vanguard_fact_handoff", False))}
        if not messages:
            return self._reference
        current = [message for message in messages.values()
                   if not getattr(message, "_vanguard_fact_handoff", False)]
        if not current:
            return self._reference
        observed = min(self._task_observed_at, self._inherited_observed_at or self._task_observed_at)
        if not 0 <= time.time()-observed < 300:
            return None
        try:
            key = uuid.uuid4().hex
            response = await self.store.save_history_page(
                self.account_id, self._group_id, key=key,
                records=[_record(message) for message in messages.values()],
                metadata={"fact_handoff_version": VERSION, "identity": self._identity},
                observed_at=observed,
            )
            if not response.ok or self.identity() != self._identity:
                return None
            # Tokens, session identifiers and message bodies never enter this ref.
            return {"version": VERSION, "key": key, "identity": self._identity}
        except (AttributeError, KeyError, ValueError, TypeError, OSError):
            return None


def create_context(account_id: int, wrapper: Any) -> QualificationReadContext:
    """Both collection entry points use this factory and the original owner."""
    from app.modules.acquisition.qualification_engine import enabled_for
    from app.modules.acquisition.review_read_flags import stage_enabled

    if enabled_for(account_id) or stage_enabled(account_id, "fact_handoff"):
        from app.modules.acquisition.review_read_runtime import maintenance_store

        store = maintenance_store(account_id)
        if store is not None:
            context = ReviewFactOwner(account_id=account_id, wrapper=wrapper, store=store)
            if context.owned():
                return context
    return QualificationReadContext(enabled=enabled_for(account_id) or stage_enabled(account_id, "reuse"))


async def bind_context(context: QualificationReadContext, entity: Any, scope: dict, previous: dict) -> None:
    if isinstance(context, ReviewFactOwner):
        await context.bind(entity, scope, previous)


async def publish_context(context: QualificationReadContext | None, snapshot: dict) -> None:
    if isinstance(context, ReviewFactOwner):
        if context.inherited_used_at is not None:
            from datetime import UTC, datetime

            from app.modules.acquisition.review_history_progress import _date

            oldest = datetime.fromtimestamp(context.inherited_used_at, UTC).replace(tzinfo=None)
            existing = _date(snapshot.get("collected_at"))
            snapshot["collected_at"] = min(existing or oldest, oldest).isoformat()
            snapshot["raw_fact_source_oldest_at"] = oldest.isoformat()
        reference = await context.publish()
        if reference is not None:
            snapshot[REFERENCE] = reference
