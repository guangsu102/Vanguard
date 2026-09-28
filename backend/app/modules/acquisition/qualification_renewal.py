"""Renew approved memberships using the same live checks as an actual send."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.core.account.rpc_governor import RpcDeferred
from app.core.account.operation_lease import AccountOperationLeaseBusy, AccountOperationLeaseUnavailable
from app.modules.acquisition.models import AdDeliveryLog
from app.modules.acquisition.qualification_lifetime import evidence_expiry, renewal_at


async def try_light_renewal(service: Any, account: Any, group: Any, membership: Any,
                            row: Any, previous: dict, *, force_refresh: bool) -> Any:
    from app.modules.acquisition.automation import JoinedGroupAuditResult
    from app.modules.acquisition.group_qualification import POLICY_VERSION, snapshot_hash
    from app.modules.acquisition.qualification_ai import profile_fingerprint
    from app.modules.acquisition.qualification_service import _date, _payload, scope_matches, policy
    from app.modules.acquisition.qualification_system_identity import covered_system_ids
    from app.modules.acquisition.qualification_actions import validate_live_send
    from app.modules.acquisition.qualification_continuity import OWN_SOURCE, own_proof_valid

    if (force_refresh or row is None or membership is None
            or row.decision not in {"allowed", "trial"}
            or membership.status != "joined" or not scope_matches(row, group, membership)
            or previous.get("policy_version") != POLICY_VERSION
            or previous.get("decision") not in {"allowed", "trial"}
            or previous.get("profile_fingerprint") != profile_fingerprint(account)
            or previous.get("technical_errors") or previous.get("invalidated_at")
            or previous.get("quality_status") != "qualified"
            or (previous.get("advertising_audit") or {}).get("ad_allowed") is not True
            or (previous.get("advertising_audit") or {}).get("policy_mode") not in {
                "soft_ad_trial", "soft_ad_allowed", "high_volume_ad_allowed"
            }):
        return None
    if previous.get("authorization_basis") == OWN_SOURCE:
        if (row.expires_at and row.expires_at > datetime.utcnow()
                and await own_proof_valid(service.db, previous, membership, group)):
            row.state = "completed"
            row.next_retry_at = row.expires_at
            return JoinedGroupAuditResult(
                passed=True, can_send_messages=True, ad_allowed=True,
                ad_rule_details={**previous["advertising_audit"], "qualification": previous},
            )
        # No fresh own observation remains. Do not loop through a live guard
        # that requires the very proof which has expired; collect fresh evidence.
        return None
    # Adverse delivery evidence requires a full review, never a quiet renewal.
    failed = await service.db.scalar(select(AdDeliveryLog.id).where(
        AdDeliveryLog.account_id == account.id, AdDeliveryLog.group_id == group.id,
        AdDeliveryLog.survival_status.in_(["deleted", "check_failed"]),
        AdDeliveryLog.created_at >= (_date(previous.get("checked_at")) or datetime.min),
    ).limit(1))
    if failed is not None:
        return None
    wrapper = None
    try:
        await service.account_pool.add_account_from_db(account)
        wrapper = await service.account_pool.acquire_by_id(
            account.id, purpose="group_qualification", raise_on_lease_failure=True,
        )
        if wrapper is None or wrapper.client is None:
            raise RpcDeferred("telegram_rpc_guard_unavailable", 60)
        me = await wrapper.client.get_me()
        _, fingerprint = await covered_system_ids(
            service.db, await policy(service.db, fresh=True),
            account_id=account.id, live_user_id=int(me.id),
        )
        if not fingerprint or fingerprint != previous.get("system_identity_fingerprint"):
            return None
        await validate_live_send(service.db, wrapper.client, account.id, group.group_id)
    except RpcDeferred as exc:
        # Preserve the old approval and its original deadline. No blind extension.
        row.state = "completed"
        row.next_retry_at = datetime.utcnow() + timedelta(seconds=exc.retry_after_seconds + 1)
        return JoinedGroupAuditResult(passed=False, reason=exc.reason)
    except (AccountOperationLeaseBusy, AccountOperationLeaseUnavailable, OSError, TimeoutError) as exc:
        row.state = "completed"
        row.next_retry_at = datetime.utcnow() + timedelta(seconds=60)
        return JoinedGroupAuditResult(passed=False, reason=type(exc).__name__)
    except Exception as exc:
        from app.core.account.telegram_execution import TelegramExecutionError
        hard_reasons = {
            "qualification_current_permission_changed", "qualification_identity_changed",
            "qualification_protected_membership", "qualification_paid_messages_not_authorized",
            "qualification_rules_changed",
        }
        if row.decision in {"allowed", "trial"} and (
            not isinstance(exc, TelegramExecutionError) or str(exc) not in hard_reasons
        ):
            row.state = "completed"
            row.next_retry_at = datetime.utcnow() + timedelta(seconds=max(
                60, int(getattr(exc, "retry_after_seconds", 0) or getattr(exc, "seconds", 0) or 60)
            ))
            return JoinedGroupAuditResult(passed=False, reason=type(exc).__name__)
        # Changes/unknown evidence go back to the full review queue. Never send here.
        now = datetime.utcnow()
        previous = _payload(row) or previous
        row.state, row.decision = "completed", "observe"
        row.reason = "qualification_renewal_requires_review:" + type(exc).__name__
        row.expires_at = min(row.expires_at or now, now)
        row.next_retry_at = now + timedelta(seconds=max(120, int(getattr(exc, "seconds", 0) or 0)))
        previous["renewal_failure_type"] = type(exc).__name__
        previous["decision"], previous["reason"] = row.decision, row.reason
        row.evidence_json = json.dumps(previous, ensure_ascii=False, default=str)
        return JoinedGroupAuditResult(passed=False, reason=row.reason)
    finally:
        if wrapper is not None:
            await service.account_pool.release(wrapper)

    now = datetime.utcnow()
    snapshot = {k: v for k, v in previous.items() if k not in {"previous_checks", "pending_collection"}}
    history = list(previous.get("previous_checks", []))
    history.append({k: v for k, v in previous.items() if k not in {"previous_checks", "qualification_exit_history"}})
    snapshot.update(
        previous_checks=history[-6:],
        full_collected_at=previous.get("full_collected_at") or previous.get("collected_at"),
        collected_at=now.isoformat(), checked_at=now.isoformat(),
        renewal_mode="live_send_checks", renewal_checked_at=now.isoformat(),
        renewal_reused_semantics=True,
    )
    row.checked_at, row.expires_at = now, evidence_expiry(now, now)
    row.state, row.next_retry_at = "completed", renewal_at(now, now)
    row.evidence_hash = snapshot_hash(snapshot, account.id, row.content_scope)
    row.evidence_json = json.dumps(snapshot, ensure_ascii=False, default=str)
    await service.db.flush()
    return JoinedGroupAuditResult(
        passed=True, reason=None, can_send_messages=True, ad_allowed=True,
        ad_rule_reason=row.reason,
        ad_rule_details={**snapshot["advertising_audit"], "qualification": snapshot},
        verification_details={"qualification_decision": row.decision, "renewal_mode": "live_send_checks"},
    )
