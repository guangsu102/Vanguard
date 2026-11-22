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
    from app.modules.acquisition.qualification_identity_refresh import reusable_identity_context
    from app.modules.acquisition.qualification_service import policy
    identity = await reusable_identity_context(db, await policy(db, fresh=True), account.id, previous)
    if not identity:
        return False
    from app.modules.acquisition.qualification_lifetime import authorization_current
    if (previous.get("system_identity_fingerprint") != identity["fingerprint"]
            and not authorization_current(row, datetime.utcnow())):
        return False
    return (previous.get("authorization_basis") != OWN_SOURCE
            or await own_proof_valid(db, previous, membership, group))


async def try_light_renewal(service: Any, account: Any, group: Any, membership: Any,
                            row: Any, previous: dict, *, force_refresh: bool) -> Any:
    if getattr(row, "_efficiency_claim", None):
        from app.modules.acquisition.qualification_engine import try_light_renewal as renew_work
        return await renew_work(service, account, group, membership, row, previous, force_refresh=force_refresh)
    from app.modules.acquisition.automation import JoinedGroupAuditResult
    from app.modules.acquisition.qualification_actions import (
        _expected_refresh_scope,
        _lock_refresh_scope,
        _refresh_scope,
        _release_refresh_transaction,
        refresh_live_authorization,
    )
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
    from app.modules.acquisition.renewal_progress import legacy_resume, resume
    from app.modules.acquisition.review_read_flags import stage_enabled
    from app.modules.acquisition.review_read_plan import apply as apply_read_plan
    from app.modules.acquisition.review_read_plan import for_review
    work = current(account.id)
    if work is not None:
        work.kind = 'renewal'
        value = await for_review(service.db, previous, row, membership, group, account,
                                 event, datetime.utcnow(), renewal=True)
        apply_read_plan(work, value, account.id, renewal=True)
    progress = ((resume if stage_enabled(account.id, "resume") else legacy_resume)(
        previous, event, membership, datetime.utcnow()) if event else None)
    wrapper = None
    shared_context = None
    refresh_scope = None
    handoff_snapshot = None
    failure = None
    try:
        if event:
            # Resolve every local handoff input before acquiring a connected
            # account. The snapshot commit permits only this audit/member/event
            # write set; unrelated (including already flushed) work is deferred.
            refresh_scope = await _refresh_scope(service.db, row, group, membership)
            from app.modules.acquisition.qualification_system_identity import covered_system_ids

            handoff_policy = refresh_scope["policy"]
            _, identity = await covered_system_ids(service.db, handoff_policy, account_id=account.id)
            await _release_refresh_transaction(service.db, row, membership)
            await service.account_pool.add_account_from_db(account)
            wrapper = await service.account_pool.acquire_by_id(
                account.id, purpose=REFRESH_PURPOSE, raise_on_lease_failure=True,
            )
            if wrapper is None or wrapper.client is None:
                raise RpcDeferred("telegram_rpc_guard_unavailable", 60)
            handoff_options = {}
            from app.modules.acquisition.review_fact_owner import ReviewFactOwner, create_context
            shared_context = create_context(account.id, wrapper)
            if isinstance(shared_context, ReviewFactOwner):
                from app.modules.acquisition.group_qualification import POLICY_VERSION
                from app.modules.acquisition.qualification_ai import profile_fingerprint
                handoff_options = {"shared_read_context": shared_context, "handoff_scope": {
                    "membership_id": membership.id,
                    "joined_at": membership.joined_at.isoformat() if membership.joined_at else None,
                    "event": {key: value for key, value in event.items() if key != "queued"},
                    "policy": {"version": POLICY_VERSION, "settings": handoff_policy},
                    "profile": profile_fingerprint(account), "system_identity": identity,
                }}
            event_evidence = []
            if (stage_enabled(account.id, "push") and event.get("evidence_refs")
                    and "gap" not in event["kinds"]):
                from app.modules.acquisition.adaptive_frequency import canonical
                from app.modules.acquisition.review_read_runtime import cached_event_messages

                marked_target = canonical(group.group_id, previous)
                if marked_target is not None:
                    event_evidence = await cached_event_messages(
                        account_id=account.id, group_id=marked_target,
                        joined_at=membership.joined_at, refs=event["evidence_refs"],
                        client=wrapper.client,
                    )
            scope_token = _expected_refresh_scope.set(refresh_scope)
            try:
                await refresh_live_authorization(
                    service.db, wrapper.client, account.id, group.group_id,
                    permissions_only=set(event["kinds"]) <= {"membership"},
                    message_ids=event.get("message_ids"),
                    recheck_rules=bool(set(event["kinds"]) & {"rules", "gap"}),
                    **({'progress': progress} if set(event['kinds']) != {'membership'} else {}),
                    **({"event_evidence": event_evidence} if event_evidence else {}),
                    **handoff_options,
                )
            finally:
                _expected_refresh_scope.reset(scope_token)
    except (RpcDeferred, AccountOperationLeaseBusy, AccountOperationLeaseUnavailable,
            OSError, TimeoutError, TelegramExecutionError) as exc:
        failure = exc
    finally:
        try:
            changed = failure is not None and str(failure) in {
                "qualification_refresh_scope_changed", "qualification_transaction_pending",
            }
            if refresh_scope is not None and not changed:
                await _release_refresh_transaction(service.db, row, membership)
            if shared_context is not None and not changed:
                from app.modules.acquisition.review_fact_owner import publish_context
                handoff_snapshot = json.loads(row.evidence_json or "{}")
                await publish_context(shared_context, handoff_snapshot)
        finally:
            if wrapper is not None:
                await service.account_pool.release(wrapper)

    if failure is not None and str(failure) in {
        "qualification_refresh_scope_changed", "qualification_transaction_pending",
    }:
        # A newer callback/claim owns the row. Do not publish the old handoff or
        # turn the new generation into this worker's budget/technical wait.
        return JoinedGroupAuditResult(passed=False, reason=str(failure))
    if refresh_scope is not None:
        try:
            row, group, membership = await _lock_refresh_scope(service.db, refresh_scope)
        except TelegramExecutionError as exc:
            if str(exc) != "qualification_refresh_scope_changed":
                raise
            return JoinedGroupAuditResult(passed=False, reason=str(exc))
    if handoff_snapshot is not None and handoff_snapshot != json.loads(row.evidence_json or "{}"):
        row.evidence_json = json.dumps(handoff_snapshot, ensure_ascii=False)
    if failure is not None:
        exc = failure
        row.state = "completed"
        delay = max(60, int(getattr(exc, "retry_after_seconds", 0) or 60))
        # A confirmed permission change waits for its recovery event. It does
        # not discard the original grant or launch repeated whole-group audits.
        terminal = str(exc) in {"qualification_current_permission_changed",
                               "qualification_protected_membership",
                               "qualification_paid_messages_not_authorized"}
        row.next_retry_at = None if terminal else datetime.utcnow() + timedelta(seconds=delay)
        from app.modules.acquisition.review_waits import clear, record
        if terminal:
            await clear(service.db, row.id)
        else:
            await record(service.db, row.id, getattr(exc, "reason", str(exc)) or type(exc).__name__,
                         0, row.next_retry_at, datetime.utcnow())
        if isinstance(exc, RpcDeferred) and (progress or {}).get('checked'):
            saved = json.loads(row.evidence_json or '{}')
            saved['renewal_progress'] = progress
            saved['renewal_wait_reason'] = exc.reason
            row.evidence_json = json.dumps(saved, ensure_ascii=False)
        return JoinedGroupAuditResult(passed=False, reason=getattr(exc, "reason", str(exc)))
    snapshot = json.loads(row.evidence_json or "{}")
    from app.modules.acquisition.qualification_identity_refresh import (
        reusable_identity_context,
        wake_identity_schedule,
    )
    from app.modules.acquisition.qualification_service import policy
    identity = await reusable_identity_context(
        service.db, await policy(service.db, fresh=True), account.id, snapshot,
    )
    if identity is None:
        row.state, row.next_retry_at = "completed", datetime.utcnow() + timedelta(minutes=5)
        from app.modules.acquisition.review_waits import record
        await record(service.db, row.id, "qualification_system_identity_unconfirmed", 0,
                     row.next_retry_at, datetime.utcnow())
        return JoinedGroupAuditResult(passed=False, reason="qualification_system_identity_unconfirmed")
    previous_identity = snapshot.get("system_identity_fingerprint")
    identity_change = ((previous_identity, identity["fingerprint"])
                       if previous_identity != identity["fingerprint"] else None)
    if identity_change is not None:
        snapshot["system_identity_revalidation"] = {
            "previous_fingerprint": snapshot.get("system_identity_fingerprint"),
            "basis": identity["basis"], "checked_at": datetime.utcnow().isoformat(),
        }
        snapshot["system_identity_fingerprint"] = identity["fingerprint"]
    snapshot["system_identity_managed_account_ids"] = identity["managed_account_ids"]
    snapshot.pop("identity_refresh", None)
    snapshot.pop('renewal_progress', None)
    snapshot.pop('renewal_wait_reason', None)
    snapshot["authorization_mode"] = "events"
    if event:
        snapshot["event_reconciled_at"] = datetime.utcnow().isoformat()
    row.evidence_json = json.dumps(snapshot, ensure_ascii=False)
    row.state, row.next_retry_at = "completed", None
    from app.core.account.review_completions import stage_completion
    await stage_completion(service.db, row, work, kind="renewal",
                           event_value=event, identity_change=identity_change)
    from app.modules.acquisition.review_waits import clear
    await clear(service.db, row.id)
    # Retain collection timestamps; processing an event is not fresh AI evidence.
    await service.db.flush()
    await wake_identity_schedule(service.db, account.id, group.id)
    return JoinedGroupAuditResult(
        passed=True, can_send_messages=True, ad_allowed=True,
        ad_rule_details={**snapshot["advertising_audit"], "qualification": snapshot},
    )
