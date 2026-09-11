from __future__ import annotations

import ast
import inspect

from sqlalchemy import UniqueConstraint

from app.core.group.models import GroupAccountMembership
from app.modules.owned_group import operations_read_model


def _select_calls(tree: ast.AST, model_name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "select"
        and model_name in ast.unparse(node)
    ]


def _attributes(call: ast.Call, model_name: str) -> set[str]:
    return {
        node.attr
        for node in ast.walk(call)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == model_name
    }


def test_read_model_does_not_import_growth_advertising_models():
    source = inspect.getsource(operations_read_model)
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    assert not {
        "AdCreative",
        "AdCampaign",
        "AccountAdBinding",
        "AdDeliveryLog",
    }.intersection(imported)


def test_account_operation_config_uses_only_explicit_mode_projection():
    tree = ast.parse(inspect.getsource(operations_read_model))
    calls = _select_calls(tree, "AccountOperationConfig")

    assert len(calls) == 1
    assert _attributes(calls[0], "AccountOperationConfig") == {
        "account_id",
        "operation_mode",
    }


def test_group_membership_projection_excludes_every_growth_column():
    tree = ast.parse(inspect.getsource(operations_read_model))
    calls = _select_calls(tree, "GroupAccountMembership")

    assert len(calls) == 1
    assert _attributes(calls[0], "GroupAccountMembership") == {
        "group_id",
        "telegram_group_id",
        "account_id",
        "status",
        "joined_at",
        "left_at",
        "last_checked_at",
        "updated_at",
    }


def test_group_membership_account_source_reference_is_stable_without_projecting_id():
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in GroupAccountMembership.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    source = inspect.getsource(operations_read_model)

    assert ("group_id", "account_id") in unique_columns
    assert "source_id=account_id" in source


def test_read_model_contains_no_session_mutation_calls():
    tree = ast.parse(inspect.getsource(operations_read_model))
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and isinstance(node.func.value.value, ast.Name)
        and node.func.value.value.id == "self"
        and node.func.value.attr == "db"
    }

    assert not {"add", "add_all", "delete", "flush", "commit"}.intersection(called_attributes)
