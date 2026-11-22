"""Invalidate only the dependencies named by an event, never acknowledge it."""
from __future__ import annotations

from typing import Any


def for_event(previous: dict[str, Any], event: dict | None) -> dict[str, Any]:
    from app.modules.acquisition.qualification_events import same_evidence_event

    old = previous.get("collection_event")
    if same_evidence_event(old, event):
        return previous
    prior = old if isinstance(old, dict) else {}
    new = event if isinstance(event, dict) else {}
    ids = {value for value in new.get("message_ids", []) if type(value) is int and value > 0}
    kinds = set(new.get("kinds") or [])
    precise = bool(
        kinds and kinds <= {"rules", "membership"}
        and "gap" not in kinds
        and prior.get("account_generation") == new.get("account_generation")
        and prior.get("unscoped_rules_token") == new.get("unscoped_rules_token")
        and prior.get("joined_at") == new.get("joined_at")
        and ("rules" not in kinds or ids)
    )
    from app.modules.acquisition.review_read_flags import stage_enabled
    if stage_enabled(previous.get("account_id"), "dependency_resume"):
        from app.modules.acquisition.review_history_dependencies import event_changes

        # Retain pointers for individually validated pages. The loader still
        # checks the persisted joining scope, protocol chain and page contents.
        precise = event_changes(old, event, joined_at=new.get("joined_at")) is not None
    result = dict(previous)
    progress = dict(previous.get("collection_progress") or {})
    # Cursor positions are traversal hints only. The collector separately
    # proves any current coverage; retaining a cursor never preserves a verdict.
    from app.modules.acquisition.qualification_engine import enabled_for
    efficiency = enabled_for(previous.get("account_id"))
    same_membership = prior.get("joined_at") == new.get("joined_at")
    if not precise and not (efficiency and same_membership):
        progress["history_cursors"] = []
    if efficiency:
        checkpoint = dict(previous.get("read_checkpoint") or {})
        # A reconnect loses live proofs, not stable metadata/body traversal.
        checkpoint.pop("own_permissions", None)
        if kinds.intersection({"membership", "gap", "rules", "author_role"}) or not same_membership:
            checkpoint.pop("role_proofs", None)
            checkpoint.pop("candidate_outcomes", None)
        if kinds.intersection({"rules", "gap"}) or not same_membership:
            checkpoint.pop("metadata", None)
            checkpoint.pop("pinned_messages", None)
        result["read_checkpoint"] = checkpoint
    progress["permission_failures"] = {}
    progress["permission_capability_retry_at"] = None
    pending = list(progress.get("pending_message_ids") or [])
    progress["pending_message_ids"] = list(dict.fromkeys([*sorted(ids), *pending]))[:100]
    result["collection_progress"] = progress
    result["collection_dependencies"] = {
        "version": 1,
        "invalidated": sorted(kinds) if precise else ["all"],
        "message_ids": sorted(ids)[:32],
        "history_positions_retained": precise,
    }
    # Old role/clear-warning/AI verdicts are never inherited for changed posts.
    result["evidence"] = [dict(item) for item in previous.get("evidence", [])
                          if item.get("message_id") not in ids]
    return result

