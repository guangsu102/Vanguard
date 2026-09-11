"""PostgreSQL-safe row-lock queries for owned-group ORM entities."""

from sqlalchemy import Select, select

from app.modules.owned_group.messaging_models import (
    GroupAccountMessageExecution,
    GroupAccountMessagePolicy,
)
from app.modules.owned_group.models import OwnedGroupAsset


def owned_group_asset_for_update_query(
    *, skip_locked: bool = False
) -> Select[tuple[OwnedGroupAsset]]:
    """Lock only the asset row despite its default joined eager relationships."""
    return select(OwnedGroupAsset).with_for_update(
        of=OwnedGroupAsset,
        skip_locked=skip_locked,
    )


def message_policy_for_update_query(
    *, skip_locked: bool = False
) -> Select[tuple[GroupAccountMessagePolicy]]:
    """Lock only the policy row despite its default joined eager relationships."""
    return select(GroupAccountMessagePolicy).with_for_update(
        of=GroupAccountMessagePolicy,
        skip_locked=skip_locked,
    )


def message_execution_for_update_query(
    *, skip_locked: bool = False
) -> Select[tuple[GroupAccountMessageExecution]]:
    """Lock only the execution row despite its default joined eager relationships."""
    return select(GroupAccountMessageExecution).with_for_update(
        of=GroupAccountMessageExecution,
        skip_locked=skip_locked,
    )
