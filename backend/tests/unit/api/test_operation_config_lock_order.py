from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from app.api.accounts import _apply_onboarding_operation_mode
from app.api.automation import (
    _get_or_create_operation_config,
    _lock_operation_configs_batch,
)
from app.core.account.models import (
    AccountOperationConfig,
    AccountOperationMode,
    AccountType,
    TelegramAccount,
)


class _OneResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _ManyResult:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values


def _postgres_sql(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def _assert_account_then_config_locks(statements) -> None:
    assert len(statements) == 2
    account_sql = _postgres_sql(statements[0])
    config_sql = _postgres_sql(statements[1])
    assert "FROM telegram_account" in account_sql
    assert "telegram_account_operation_config" not in account_sql
    assert account_sql.rstrip().endswith("FOR UPDATE OF telegram_account")
    assert "FROM telegram_account_operation_config" in config_sql
    assert config_sql.rstrip().endswith(
        "FOR UPDATE OF telegram_account_operation_config"
    )


@pytest.mark.asyncio
async def test_single_operation_config_update_locks_account_before_config():
    account = TelegramAccount(
        id=17,
        identifier="lock-single",
        session_name="lock-single",
        account_type=AccountType.PROMOTER,
    )
    config = AccountOperationConfig(
        account_id=17,
        operation_mode=AccountOperationMode.GROWTH.value,
    )
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_OneResult(account), _OneResult(config)])

    result = await _get_or_create_operation_config(
        db,
        17,
        commit_created=False,
    )

    assert result is config
    _assert_account_then_config_locks(
        [call.args[0] for call in db.execute.await_args_list]
    )
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_onboarding_mode_write_uses_the_same_account_then_config_lock_order():
    account = TelegramAccount(
        id=18,
        identifier="lock-onboarding",
        session_name="lock-onboarding",
        account_type=AccountType.PROMOTER,
    )
    config = AccountOperationConfig(
        account_id=18,
        operation_mode=AccountOperationMode.GROWTH.value,
    )
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_OneResult(account), _OneResult(config)])

    result = await _apply_onboarding_operation_mode(
        db,
        account,
        AccountOperationMode.GROWTH.value,
    )

    assert result is config
    _assert_account_then_config_locks(
        [call.args[0] for call in db.execute.await_args_list]
    )


@pytest.mark.asyncio
async def test_batch_locks_all_accounts_then_configs_in_ascending_order():
    accounts = [
        TelegramAccount(
            id=account_id,
            identifier=f"lock-batch-{account_id}",
            session_name=f"lock-batch-{account_id}",
            account_type=AccountType.PROMOTER,
        )
        for account_id in (2, 5, 9)
    ]
    configs = [
        AccountOperationConfig(
            account_id=account_id,
            operation_mode=AccountOperationMode.GROWTH.value,
        )
        for account_id in (2, 9)
    ]
    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[_ManyResult(accounts), _ManyResult(configs)]
    )
    db.flush = AsyncMock()

    result, errors = await _lock_operation_configs_batch(db, [9, 2, 5, 9])

    assert errors == {}
    assert set(result) == {2, 5, 9}
    account_sql, config_sql = [
        _postgres_sql(call.args[0]) for call in db.execute.await_args_list
    ]
    assert "ORDER BY telegram_account.id ASC" in account_sql
    assert "telegram_account.id IN (2, 5, 9)" in account_sql
    assert account_sql.rstrip().endswith("FOR UPDATE OF telegram_account")
    assert "ORDER BY telegram_account_operation_config.account_id ASC" in config_sql
    assert "telegram_account_operation_config.account_id IN (2, 5, 9)" in config_sql
    assert config_sql.rstrip().endswith(
        "FOR UPDATE OF telegram_account_operation_config"
    )
    db.add.assert_called_once()
    db.flush.assert_awaited_once()
