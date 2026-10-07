"""Estimate a useful review slice without treating partial evidence as approval."""

from datetime import datetime, timedelta
from typing import Any


def review_shape(previous: dict, row: Any, member: Any, account: Any, event: dict | None,
                 now: datetime, *, force_refresh: bool = False) -> tuple[str, int]:
    from app.modules.acquisition.evidence_progress import date
    from app.modules.acquisition.group_qualification import POLICY_VERSION
    from app.modules.acquisition.qualification_ai import profile_fingerprint
    from app.modules.acquisition.qualification_events import same_evidence_event

    if (force_refresh or row is None or member is None or account is None
            or row.membership_joined_at != member.joined_at
            or row.membership_id != member.id or row.account_id != account.id
            or previous.get('account_id') != account.id
            or previous.get('policy_version') != POLICY_VERSION
            or previous.get('profile_fingerprint') != profile_fingerprint(account)
            or not same_evidence_event(previous.get('collection_event'), event)):
        return 'full', 24
    rights = previous.get('permissions') or {}
    if (member.review_status == 'exit_pending' and member.joined_at
            and member.joined_at <= now - timedelta(hours=48)
            and rights.get('permanent_send_restriction_verified') is True
            and not rights.get('temporary_until') and not rights.get('slowmode_until')):
        # The collector already finishes immediately after fresh full-info and
        # self-permission checks when this condition remains true.
        return 'exit', 12
    collected = date(previous.get('collected_at'))
    progress = previous.get('collection_progress') or {}
    pending = progress.get('pending_message_ids')
    if (collected and now - timedelta(hours=24) <= collected <= now
            and isinstance(pending, list) and progress.get('history_cursors')
            and all(type(value) is int and value > 0 for value in pending)):
        # Fresh metadata/permissions/history are still checked. Hints only bound
        # the remaining identity work; a changed result can exhaust this slice.
        return 'continuation', min(48, 16 + 2 * min(16, len(set(pending))))
    return 'full', 24
