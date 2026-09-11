"""PostgreSQL DDL coverage for stage-2 messaging schema contracts."""

import importlib.util
import io
import re
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.core.account.models import TelegramAccount  # noqa: F401
from app.core.database import Base
from app.core.group.models import Group  # noqa: F401
from app.modules.acquisition.models import AcquisitionMessage, MessageTemplate
from app.modules.owned_group.messaging_models import (
    GroupAccountMessageExecution,
    GroupAccountMessagePolicy,
)
from app.modules.owned_group.models import OwnedGroupAsset  # noqa: F401

BACKEND_ROOT = Path(__file__).resolve().parents[4]
EXPECTED_CANONICAL_NAMES = {
    "ck_group_account_message_execution_message_execution_trigger_ty",
    "ck_group_account_message_execution_message_execution_attempt_co",
    "fk_acquisition_message_template_owned_group_asset_id_owned_grou",
    "fk_acquisition_message_owned_group_execution_id_group_account_m",
}


def _postgresql_ddl(table) -> str:
    return str(CreateTable(table).compile(dialect=postgresql.dialect()))


def _constraint_names(ddl: str) -> set[str]:
    return {
        name.strip('"')
        for name in re.findall(r"CONSTRAINT\s+([^\s]+)", ddl, flags=re.IGNORECASE)
    }


def test_model_postgresql_ddl_matches_deployed_contract() -> None:
    ddl = "\n".join(
        _postgresql_ddl(table)
        for table in (
            GroupAccountMessagePolicy.__table__,
            GroupAccountMessageExecution.__table__,
            MessageTemplate.__table__,
            AcquisitionMessage.__table__,
        )
    )
    names = _constraint_names(ddl)

    assert "trigger_config JSONB DEFAULT" in ddl
    assert "promotion_config JSONB DEFAULT" in ddl
    assert '"version"NULL' not in ddl
    assert '"enabled"NULL' not in ddl
    assert EXPECTED_CANONICAL_NAMES <= names
    assert all(len(name.encode("utf-8")) <= 63 for name in names)


def test_alembic_034_compiles_as_valid_postgresql_offline_sql() -> None:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={
            "as_sql": True,
            "output_buffer": output,
            "target_metadata": Base.metadata,
        },
    )
    path = BACKEND_ROOT / "migrations" / "versions" / "034_add_owned_group_messaging.py"
    spec = importlib.util.spec_from_file_location("migration_034_schema_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(context)

    module.upgrade()
    ddl = output.getvalue()
    names = _constraint_names(ddl)

    assert "trigger_config JSONB DEFAULT" in ddl
    assert "promotion_config JSONB DEFAULT" in ddl
    assert '"version"NULL' not in ddl
    assert '"enabled"NULL' not in ddl
    assert EXPECTED_CANONICAL_NAMES <= names
    assert all(len(name.encode("utf-8")) <= 63 for name in names)
