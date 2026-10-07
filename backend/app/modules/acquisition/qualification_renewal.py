"""Reuse initial qualification; refresh reads only follow a scoped event."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from app.core.account.operation_lease import (
    AccountOperationLeaseBusy,
    AccountOperationLeaseUnavailable,
)
from app.core.account.rpc_governor import RpcDeferred
from app.core.account.telegram_execution import TelegramExecutionError


REFRESH_PURPOSE = "ad_qualification_refresh"


async def eligible(db: Any, account: Any, group: Any, membership: Any,
                   row: Any, previous: dict) -> bool:
    """Use the ad reservation only to maintain an existing, scoped grant."""
    from app.modules.acquisition.group_qualification import POLICY_VERSION
    from app.modules.acquisition.qualification_ai import profile_fingerprint
    from app.modules.acquisition.qualification_continuity import OWN_SOURCE, own_proof_valid
    from app.modules.acquisition.qualification_service import scope_matches

    if (row is None or membership is None or account is None
            or row.decision not in {"allowed", "trial"}
            or membership.status != "joined" or not scope_matches(row, group, membership)
            or membership.ad_status == "paused"
            or membership.review_status in {"manual_required", "owned_group_excluded", "exit_pending"}
            or previous.get("policy_version") != POLICY_VERSION
            or previous.get("profile_fingerprint") != profile_fingerprint(account)
            or previous.get("technical_errors") or previous.get("invalidated_at")
            or previous.get("ai_pending") or previous.get("ai_review_incomplete")
            or previous.get("quality_status") != "qualified"
            or (previous.get("advertising_audit") or {}).get("ad_allowed") is not True):
        return False
    return (previous.get("authorization_basis") != OWN_SOURCE
            or await own_proof_valid(db, previous, membership, group))


async def try_light_renewal(service: Any, account: Any, group: Any, membership: Any,
                            row: Any, previous: dict, *, force_refresh: bool) -> Any:
    from app.modules.acquisition.automation import JoinedGroupAuditResult
    from app.modules.acquisition.qualification_actions import refresh_live_authorization
    from app.modules.acquisition.qualification_events import pending

    if membership is not None and (membership.ad_status == "paused"
            or membership.review_status in {"manual_required", "owned_group_excluded"}
            or (membership.review_status == "exit_pending" and not force_refresh)):
        if row is not None:
            row.state, row.next_retry_at = "completed", None
        return JoinedGroupAuditResult(passed=False, reason="qualification_membership_held")
    if force_refresh or not await eligible(service.db, account, group, membership, row, previous):
        return None
    event = await pending(service.db, membership)
    from app.core.account.read_work import current
    from app.modules.acquisition.renewal_progress import resume
    work = current(account.id)
    if work is not None:
        work.kind = 'renewal'
    progress = resume(previous, event, membership, datetime.utcnow()) if event else None
    wrapper = None
    try:
        if event:
            await service.account_pool.add_account_from_db(account)
            wrapper = await service.account_pool.acquire_by_id(
                account.id, purpose=REFRESH_PURPOSE, raise_on_lease_failure=True,
            )
            if wrapper is None or wrapper.client is None:
                raise RpcDeferred("telegram_rpc_guard_unavailable", 60)
            await refresh_live_authorization(
                service.db, wrapper.client, account.id, group.group_id,
                permissions_only=set(event["kinds"]) <= {"membership"},
                message_ids=event.get("message_ids"),
                recheck_rules=bool(set(event["kinds"]) & {"rules", "gap"}),
                **({'progress': progress} if set(event['kinds']) != {'membership'} else {}),
            )
    except (RpcDeferred, AccountOperationLeaseBusy, AccountOperationLeaseUnavailable,
            OSError, TimeoutError, TelegramExecutionError) as exc:
        row.state = "completed"
        delay = max(60, int(getattr(exc, "retry_after_seconds", 0) or 60))
        # A confirmed permission change waits for its recovery event. It does
        # not discard the original grant or launch repeated whole-group audits.
        terminal = str(exc) in {"qualification_current_permission_changed",
                               "qualification_protected_membership",
                               "qualification_paid_messages_not_authorized"}
        row.next_retry_at = None if terminal else datetime.utcnow() + timedelta(seconds=delay)
        if isinstance(exc, RpcDeferred) and (progress or {}).get('checked'):
            saved = json.loads(row.evidence_json or '{}')
            saved['renewal_progress'] = progress
            saved['renewal_wait_reason'] = exc.reason
            row.evidence_json = json.dumps(saved, ensure_ascii=False)
        return JoinedGroupAuditResult(passed=False, reason=getattr(exc, "reason", str(exc)))
    finally:
        if wrapper is not None:
            await service.account_pool.release(wrapper)

    snapshot = json.loads(row.evidence_json or "{}")
    snapshot.pop('renewal_progress', None)
    snapshot.pop('renewal_wait_reason', None)
    snapshot["authorization_mode"] = "events"
    if event:
        snapshot["event_reconciled_at"] = datetime.utcnow().isoformat()
    row.evidence_json = json.dumps(snapshot, ensure_ascii=False)
    row.state, row.next_retry_at = "completed", None
    # Retain collection timestamps; processing an event is not fresh AI evidence.
    await service.db.flush()
    return JoinedGroupAuditResult(
        passed=True, can_send_messages=True, ad_allowed=True,
        ad_rule_details={**snapshot["advertising_audit"], "qualification": snapshot},
    )
