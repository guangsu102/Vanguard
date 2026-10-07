"""Shared read-only scheduling rules used by the dispatcher and capacity view."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, and_, or_, select

from app.core.group.models import GroupAccountMembership
from app.modules.acquisition.models import AdDeliveryScheduleState


@dataclass(frozen=True)
class AdReadiness:
    reason: str | None = None
    next_allowed_at: datetime | None = None


def schedule_readiness(state: AdDeliveryScheduleState | None, now: datetime) -> AdReadiness:
    if state is None:
        return AdReadiness()
    if state.status == "paused":
        return AdReadiness(state.last_reason or "delivery_schedule_paused")
    if state.status == "sending" and state.lease_expires_at and state.lease_expires_at > now:
        return AdReadiness(
            "delivery_tuple_inflight", max(state.lease_expires_at, state.next_due_at)
        )
    if state.next_due_at > now:
        return AdReadiness("delivery_schedule_not_due", state.next_due_at)
    return AdReadiness()


def due_membership_query(
    account_id: int, campaign_id: int | None, now: datetime
) -> Select[tuple[GroupAccountMembership]]:
    """Filter indexed schedule deadlines before loading/qualifying memberships."""
    query = select(GroupAccountMembership).where(
        GroupAccountMembership.account_id == account_id,
        GroupAccountMembership.status == "joined",
        GroupAccountMembership.review_status == "approved",
        or_(
            GroupAccountMembership.ad_status.is_(None),
            GroupAccountMembership.ad_status != "blocked",
        ),
        or_(
            GroupAccountMembership.ad_pause_until.is_(None),
            GroupAccountMembership.ad_pause_until <= now,
        ),
    )
    if campaign_id is None:
        return query
    state = AdDeliveryScheduleState
    return query.outerjoin(
        state,
        and_(
            state.account_id == account_id,
            state.campaign_id == campaign_id,
            state.group_id == GroupAccountMembership.group_id,
        ),
    ).where(
        or_(
            state.id.is_(None),
            and_(
                state.status != "paused",
                state.next_due_at <= now,
                or_(
                    state.status != "sending",
                    state.lease_expires_at.is_(None),
                    state.lease_expires_at <= now,
                ),
            ),
        )
    )
