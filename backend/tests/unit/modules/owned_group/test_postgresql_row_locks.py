"""PostgreSQL compilation coverage for eager-loaded owned-group row locks."""

import pytest
from sqlalchemy.dialects import postgresql

# Register related mappers before compiling ORM statements with eager joins.
from app.core.account.models import TelegramAccount  # noqa: F401
from app.core.campaign.models import Campaign  # noqa: F401
from app.core.group.models import Group  # noqa: F401
from app.core.keyword.models import Keyword  # noqa: F401
from app.core.user.models import User  # noqa: F401
from app.modules.acquisition.models import KeywordTrigger, MessageTemplate  # noqa: F401
from app.modules.guardian.models import ManagedGroupBinding  # noqa: F401
from app.modules.owned_group.lock_queries import (
    message_execution_for_update_query,
    message_policy_for_update_query,
    owned_group_asset_for_update_query,
)
from app.modules.owned_group.models_extra import OwnedGroupMembership  # noqa: F401


@pytest.mark.parametrize(
    ("query_factory", "table_name", "skip_locked"),
    [
        (owned_group_asset_for_update_query, "owned_group_assets", False),
        (message_policy_for_update_query, "group_account_message_policy", False),
        (message_execution_for_update_query, "group_account_message_execution", False),
        (message_execution_for_update_query, "group_account_message_execution", True),
    ],
)
def test_eager_loaded_row_locks_target_only_primary_table_on_postgresql(
    query_factory,
    table_name: str,
    skip_locked: bool,
) -> None:
    statement = query_factory(skip_locked=skip_locked)

    sql = " ".join(str(statement.compile(dialect=postgresql.dialect())).split())

    # Keep eager-loading in the query so this exercises PostgreSQL's nullable
    # outer-join restriction instead of hiding it by disabling relationships.
    assert "LEFT OUTER JOIN" in sql
    assert f"FOR UPDATE OF {table_name}" in sql
    assert ("SKIP LOCKED" in sql) is skip_locked
