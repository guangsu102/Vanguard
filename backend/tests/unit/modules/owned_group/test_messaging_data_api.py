import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError
from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession, StringSession

from app.api.acquisition import (
    MessageTemplateUpdate,
    list_message_templates,
    update_message_template,
)
from app.api.acquisition import router as acquisition_router
from app.api.owned_group_messages import router
from app.core.account.models import (
    AccountOperationMode,
    AccountRiskLevel,
    AccountStatus,
    AccountType,
)
from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user, require_admin
from app.modules.acquisition.auto_reply.templates import TemplateEngine
from app.modules.acquisition.models import MessageTemplate, MessageType
from app.modules.owned_group import messaging_policy_service as policy_service_module
from app.modules.owned_group.messaging_contracts import OwnedGroupMessagingError
from app.modules.owned_group.messaging_models import (
    GroupAccountMessageExecution,
    GroupAccountMessagePolicy,
)
from app.modules.owned_group.messaging_policy_service import (
    OwnedGroupMessagingPolicyService,
)
from app.modules.owned_group.messaging_schemas import (
    ExecutionApprove,
    OwnedGroupTemplateWrite,
    PolicyCreate,
    PolicyUpdate,
)
from app.modules.owned_group.messaging_target import MembershipProbeResult


def _valid_string_session() -> str:
    session = StringSession()
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(bytes(range(256)))
    return session.save()


def test_stage_two_models_and_router_contract_are_registered() -> None:
    assert len(router.routes) == 14
    assert {
        "owned_group_asset_id",
        "core_group_id",
        "account_id",
        "trigger_config",
        "promotion_config",
        "revision",
    } <= set(GroupAccountMessagePolicy.__table__.columns.keys())
    assert {
        "telegram_chat_id",
        "message_purpose",
        "content_category",
        "content_hash",
        "idempotency_key",
        "correlation_id",
        "lease_expires_at",
        "write_started_at",
        "reviewed_at",
    } <= set(GroupAccountMessageExecution.__table__.columns.keys())

    admin_only = {
        ("POST", "/{asset_id}/messages/policies"),
        ("PUT", "/{asset_id}/messages/policies/{policy_id}"),
        ("POST", "/{asset_id}/messages/policies/{policy_id}/executions"),
        ("POST", "/{asset_id}/messages/executions/{execution_id}/approve"),
        ("POST", "/{asset_id}/messages/executions/{execution_id}/reject"),
        ("POST", "/{asset_id}/messages/templates"),
        ("PUT", "/{asset_id}/messages/templates/{template_id}"),
    }
    found: set[tuple[str, str]] = set()
    for route_item in router.routes:
        calls = {dependency.call for dependency in route_item.dependant.dependencies}
        for method in route_item.methods:
            key = (method, route_item.path)
            if key in admin_only:
                found.add(key)
                assert require_admin in calls
    assert found == admin_only


def test_acquisition_writes_that_control_stage_two_require_admin() -> None:
    protected = {
        ("POST", "/messages"),
        ("POST", "/message-templates"),
        ("PUT", "/message-templates/{template_id}"),
        ("DELETE", "/message-templates/{template_id}"),
        ("POST", "/keyword-triggers"),
        ("POST", "/keyword-triggers/generate"),
        ("POST", "/keyword-triggers/batch-template"),
        ("PUT", "/keyword-triggers/{trigger_id}"),
        ("DELETE", "/keyword-triggers/{trigger_id}"),
    }
    found: set[tuple[str, str]] = set()
    for route_item in acquisition_router.routes:
        calls = {dependency.call for dependency in route_item.dependant.dependencies}
        for method in route_item.methods:
            key = (method, route_item.path)
            if key in protected:
                found.add(key)
                assert require_admin in calls
    assert found == protected


def test_all_message_routes_publish_typed_success_and_error_openapi() -> None:
    app = FastAPI()
    app.include_router(router, prefix="/api/owned-groups")
    openapi = app.openapi()
    operations = [
        operation
        for path in openapi["paths"].values()
        for method, operation in path.items()
        if method in {"get", "post", "put"}
    ]
    assert len(operations) == 14
    for operation in operations:
        success_code = next(
            code for code in ("200", "201", "202") if code in operation["responses"]
        )
        success_schema = operation["responses"][success_code]["content"]["application/json"][
            "schema"
        ]
        assert success_schema.get("$ref", "").startswith("#/components/schemas/")
        assert not success_schema["$ref"].endswith("MessagingSuccessEnvelope")
        for error_code in (
            "400",
            "401",
            "403",
            "404",
            "409",
            "422",
            "429",
            "500",
            "503",
        ):
            error_schema = operation["responses"][error_code]["content"]["application/json"][
                "schema"
            ]
            assert error_schema["$ref"].endswith("MessagingErrorEnvelope")

    schemas = openapi["components"]["schemas"]
    assert set(schemas["EligibleAccountsEnvelope"]["properties"]) == {
        "data",
        "asset",
        "correlation_id",
    }
    assert set(schemas["ExecutionPageResponse"]["properties"]) == {
        "total",
        "page",
        "page_size",
        "items",
    }
    assert {"error", "correlation_id"} <= set(schemas["MessagingErrorEnvelope"]["required"])


def test_policy_schema_normalizes_lists_and_blocks_invalid_combinations() -> None:
    policy = PolicyCreate(
        account_id=7,
        mode="ai",
        allowed_topics=[" Topic ", "topic", "另一个"],
        trigger_config={
            "scheduled": {
                "enabled": True,
                "times": ["18:30", "10:30", "10:30"],
                "weekdays": [5, 1, 1],
                "content_category": "community",
            }
        },
    )
    assert policy.allowed_topics == ["Topic", "另一个"]
    assert policy.trigger_config.scheduled.times == ["10:30", "18:30"]
    assert policy.trigger_config.scheduled.weekdays == [1, 5]

    with pytest.raises(ValueError):
        PolicyCreate(
            account_id=7,
            mode="off",
            trigger_config={
                "scheduled": {
                    "enabled": True,
                    "times": ["10:30"],
                    "content_category": "community",
                }
            },
        )
    with pytest.raises(ValueError):
        PolicyUpdate(revision=1)


@pytest.mark.asyncio
async def test_safe_disable_does_not_require_current_account_eligibility() -> None:
    service = OwnedGroupMessagingPolicyService(AsyncMock())
    service._require_account_eligible = AsyncMock()
    service.target_resolver.resolve = AsyncMock()

    await service._validate_enabling(1, 2, enabled=False)

    service._require_account_eligible.assert_not_awaited()
    service.target_resolver.resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabling_policy_cancels_unsent_execution_and_drops_raw_prompt(
    monkeypatch,
) -> None:
    policy = SimpleNamespace(
        id=15,
        owned_group_asset_id=901,
        core_group_id=801,
        account_id=7,
        mode="ai",
        default_template_id=None,
        trigger_config={},
        promotion_config={"mode": "off"},
        daily_limit=5,
        cooldown_seconds=3600,
        allowed_topics=["support"],
        require_review=True,
        enabled=True,
        revision=1,
    )
    execution = SimpleNamespace(
        id=501,
        prompt_context={
            "instruction": "private instruction",
            "recent_context": [{"text": "private member text"}],
            "request_fingerprint": "fingerprint-1",
            "source_text": "raw source",
            "variables": {"group_name": "private group"},
        },
        status="generating",
        lease_id="generation-lease",
        lease_expires_at=datetime.utcnow() + timedelta(minutes=1),
        write_started_at=None,
        next_retry_at=datetime.utcnow() + timedelta(minutes=2),
        revision=3,
        updated_at=datetime.utcnow(),
    )
    db = MagicMock()
    db.execute = AsyncMock(return_value=SimpleNamespace(rowcount=1))
    db.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: [execution]))
    db.refresh = AsyncMock()
    db.flush = AsyncMock()
    events: list[str] = []
    db.commit = AsyncMock(side_effect=lambda: events.append("commit"))
    db.add = MagicMock()
    prompt_context_store = SimpleNamespace(
        discard=AsyncMock(side_effect=lambda execution_id: events.append("discard"))
    )
    service = OwnedGroupMessagingPolicyService(
        db,
        prompt_context_store=prompt_context_store,
    )
    service._asset = AsyncMock(return_value=SimpleNamespace(telegram_chat_id=-1000000000901))
    service.get_policy = AsyncMock(return_value=policy)
    service._validate_enabling = AsyncMock()
    service._validate_policy_references = AsyncMock()
    service._policy_response = AsyncMock(return_value={"policy_id": 15})
    lock = AsyncMock()
    monkeypatch.setattr(
        policy_service_module,
        "acquire_telegram_chat_transaction_lock",
        lock,
    )

    result = await service.update_policy(
        901,
        15,
        PolicyUpdate(
            revision=1,
            mode="ai",
            default_template_id=None,
            trigger_config={},
            promotion_config={"mode": "off"},
            daily_limit=5,
            cooldown_seconds=3600,
            allowed_topics=["support"],
            require_review=True,
            enabled=False,
        ),
        actor={"id": 1},
        correlation_id="disable-policy",
    )

    assert result == {"policy_id": 15}
    assert execution.status == "cancelled"
    assert execution.revision == 4
    assert execution.lease_id is None
    assert execution.lease_expires_at is None
    assert execution.write_started_at is None
    assert execution.next_retry_at is None
    assert execution.prompt_context["request_fingerprint"] == "fingerprint-1"
    assert execution.prompt_context["recent_context_message_count"] == 1
    assert execution.prompt_context["variable_keys"] == ["group_name"]
    for raw_key in ("instruction", "recent_context", "source_text", "variables"):
        assert raw_key not in execution.prompt_context
    prompt_context_store.discard.assert_awaited_once_with(501)
    lock.assert_awaited_once_with(db, -1000000000901)
    assert events == ["commit", "discard"]


@pytest.mark.asyncio
async def test_ineligible_account_can_save_disabled_draft_but_cannot_enable(
    monkeypatch,
) -> None:
    db = MagicMock()
    db.scalar = AsyncMock(return_value=None)
    db.flush = AsyncMock()
    service = OwnedGroupMessagingPolicyService(db)
    service._asset = AsyncMock(return_value=SimpleNamespace(id=901, core_group_id=801))
    service._validate_policy_references = AsyncMock()
    service._policy_response = AsyncMock(return_value={"id": 15, "enabled": False})
    blocked = OwnedGroupMessagingError(
        "ACCOUNT_NOT_ELIGIBLE",
        "blocked",
        details={"blocking_reasons": ["account_session_missing"]},
    )
    service._require_account_eligible = AsyncMock(side_effect=blocked)
    service.target_resolver.resolve = AsyncMock(return_value=object())
    monkeypatch.setattr(settings, "OWNED_GROUP_MESSAGING_ENABLED", True)

    response = await service.create_policy(
        901,
        PolicyCreate(account_id=7, enabled=False),
        actor={"id": 1},
        correlation_id="disabled-draft",
    )

    assert response == {"id": 15, "enabled": False}
    service._require_account_eligible.assert_not_awaited()

    with pytest.raises(OwnedGroupMessagingError) as exc_info:
        await service.create_policy(
            901,
            PolicyCreate(account_id=7, enabled=True),
            actor={"id": 1},
            correlation_id="enable-blocked",
        )

    assert exc_info.value.code == "ACCOUNT_NOT_ELIGIBLE"
    service._require_account_eligible.assert_awaited_once_with(901, 7)


def _stale_eligible_account() -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        account_type=AccountType.PROMOTER,
        is_active=True,
        status=AccountStatus.ONLINE,
        risk_level=AccountRiskLevel.NORMAL.value,
        risk_pause_until=None,
        operation_config=SimpleNamespace(
            enabled=True,
            operation_mode=AccountOperationMode.GROWTH.value,
        ),
        display_name="sender",
        identifier="sender-7",
        session_string=_valid_string_session(),
        session_name="",
    )


def _write_valid_sqlite_session(directory: Path, session_name: str) -> Path:
    session = SQLiteSession(str(directory / session_name))
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(bytes(range(256)))
    session.save()
    session.close()
    return directory / f"{session_name}.session"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_string",
    ["", "legacy-session", "vgs1:not-valid-ciphertext"],
)
async def test_missing_or_undecryptable_session_is_reported_without_probe(
    session_string: str,
) -> None:
    now = datetime.utcnow()
    account = _stale_eligible_account()
    account.session_string = session_string
    membership = SimpleNamespace(
        resource_type="user",
        resource_id=account.id,
        status="member_verified",
        last_verified_at=now,
        telegram_user_id=7007,
    )
    db = AsyncMock()
    db.scalar.side_effect = [account, membership, None]
    probe = AsyncMock()
    service = OwnedGroupMessagingPolicyService(db)
    service.target_resolver.membership_probe = probe

    eligibility = await service.target_resolver.validate_account_eligibility(
        901,
        account.id,
        probe_stale=True,
    )

    assert eligibility.eligible is False
    assert "account_session_missing" in eligibility.blocking_reasons
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_mounted_session_file_is_accepted_without_telegram_probe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session_name = "owned-group-sender"
    _write_valid_sqlite_session(tmp_path, session_name)
    monkeypatch.setattr(settings, "TELEGRAM_SESSION_DIR", str(tmp_path))
    now = datetime.utcnow()
    account = _stale_eligible_account()
    account.session_string = ""
    account.session_name = session_name
    membership = SimpleNamespace(
        resource_type="user",
        resource_id=account.id,
        status="member_verified",
        last_verified_at=now,
        telegram_user_id=7007,
    )
    db = AsyncMock()
    db.scalar.side_effect = [account, membership, None]
    probe = AsyncMock()
    service = OwnedGroupMessagingPolicyService(db)
    service.target_resolver.membership_probe = probe

    eligibility = await service.target_resolver.validate_account_eligibility(
        901,
        account.id,
        probe_stale=True,
    )

    assert eligibility.eligible is True
    assert "account_session_missing" not in eligibility.blocking_reasons
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_mounted_session_rejects_short_auth_key(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session_name = "short-key-owned-group-sender"
    session_path = _write_valid_sqlite_session(tmp_path, session_name)
    connection = sqlite3.connect(session_path)
    connection.execute("UPDATE sessions SET auth_key = ?", (sqlite3.Binary(b"x"),))
    connection.commit()
    connection.close()
    monkeypatch.setattr(settings, "TELEGRAM_SESSION_DIR", str(tmp_path))
    account = _stale_eligible_account()
    account.session_string = ""
    account.session_name = session_name
    membership = SimpleNamespace(
        resource_type="user",
        resource_id=account.id,
        status="member_verified",
        last_verified_at=datetime.utcnow(),
        telegram_user_id=7007,
    )
    db = AsyncMock()
    db.scalar.side_effect = [account, membership, None]
    probe = AsyncMock()
    service = OwnedGroupMessagingPolicyService(db)
    service.target_resolver.membership_probe = probe

    eligibility = await service.target_resolver.validate_account_eligibility(
        901,
        account.id,
        probe_stale=True,
    )

    assert eligibility.eligible is False
    assert "account_session_missing" in eligibility.blocking_reasons
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_mounted_session_rejects_incomplete_telethon_schema(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session_name = "incomplete-owned-group-sender"
    connection = sqlite3.connect(tmp_path / f"{session_name}.session")
    connection.execute("CREATE TABLE version (version INTEGER PRIMARY KEY)")
    connection.execute("INSERT INTO version (version) VALUES (8)")
    connection.execute("CREATE TABLE sessions (dc_id INTEGER PRIMARY KEY, auth_key BLOB)")
    connection.execute(
        "INSERT INTO sessions (dc_id, auth_key) VALUES (?, ?)",
        (2, sqlite3.Binary(bytes(range(256)))),
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(settings, "TELEGRAM_SESSION_DIR", str(tmp_path))
    account = _stale_eligible_account()
    account.session_string = ""
    account.session_name = session_name
    membership = SimpleNamespace(
        resource_type="user",
        resource_id=account.id,
        status="member_verified",
        last_verified_at=datetime.utcnow(),
        telegram_user_id=7007,
    )
    db = AsyncMock()
    db.scalar.side_effect = [account, membership, None]
    probe = AsyncMock()
    service = OwnedGroupMessagingPolicyService(db)
    service.target_resolver.membership_probe = probe

    eligibility = await service.target_resolver.validate_account_eligibility(
        901,
        account.id,
        probe_stale=True,
    )

    assert eligibility.eligible is False
    assert "account_session_missing" in eligibility.blocking_reasons
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_mounted_session_rejects_path_traversal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    outside_name = "outside-owned-group-sender"
    _write_valid_sqlite_session(tmp_path, outside_name)
    monkeypatch.setattr(settings, "TELEGRAM_SESSION_DIR", str(session_dir))
    account = _stale_eligible_account()
    account.session_string = ""
    account.session_name = f"../{outside_name}"
    membership = SimpleNamespace(
        resource_type="user",
        resource_id=account.id,
        status="member_verified",
        last_verified_at=datetime.utcnow(),
        telegram_user_id=7007,
    )
    db = AsyncMock()
    db.scalar.side_effect = [account, membership, None]
    probe = AsyncMock()
    service = OwnedGroupMessagingPolicyService(db)
    service.target_resolver.membership_probe = probe

    eligibility = await service.target_resolver.validate_account_eligibility(
        901,
        account.id,
        probe_stale=True,
    )

    assert eligibility.eligible is False
    assert "account_session_missing" in eligibility.blocking_reasons
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_mounted_session_file_is_rejected_without_telegram_probe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session_name = "empty-owned-group-sender"
    (tmp_path / f"{session_name}.session").touch()
    monkeypatch.setattr(settings, "TELEGRAM_SESSION_DIR", str(tmp_path))
    account = _stale_eligible_account()
    account.session_string = ""
    account.session_name = session_name
    membership = SimpleNamespace(
        resource_type="user",
        resource_id=account.id,
        status="member_verified",
        last_verified_at=datetime.utcnow(),
        telegram_user_id=7007,
    )
    db = AsyncMock()
    db.scalar.side_effect = [account, membership, None]
    probe = AsyncMock()
    service = OwnedGroupMessagingPolicyService(db)
    service.target_resolver.membership_probe = probe

    eligibility = await service.target_resolver.validate_account_eligibility(
        901,
        account.id,
        probe_stale=True,
    )

    assert eligibility.eligible is False
    assert "account_session_missing" in eligibility.blocking_reasons
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_membership_is_probed_and_refreshed_before_enable(monkeypatch) -> None:
    before = datetime.utcnow() - timedelta(hours=25)
    account = _stale_eligible_account()
    membership = SimpleNamespace(
        resource_type="user",
        resource_id=account.id,
        status="member_verified",
        last_verified_at=before,
        telegram_user_id=7007,
        updated_at=before,
    )
    asset = SimpleNamespace(id=901, telegram_chat_id=-1000000000901)
    db = AsyncMock()
    db.scalar.side_effect = [account, membership, None]
    db.get.return_value = asset
    probe = AsyncMock(
        return_value=MembershipProbeResult(
            verified=True,
            telegram_user_id=7007,
        )
    )
    service = OwnedGroupMessagingPolicyService(db)
    service.target_resolver.membership_probe = probe
    service.target_resolver.resolve = AsyncMock(return_value=object())
    monkeypatch.setattr(settings, "OWNED_GROUP_MESSAGING_ENABLED", True)

    await service._validate_enabling(asset.id, account.id, enabled=True)

    probe.assert_awaited_once_with(asset=asset, account=account)
    db.flush.assert_awaited_once()
    assert membership.last_verified_at > before
    assert membership.telegram_user_id == 7007


@pytest.mark.asyncio
async def test_unbound_membership_identity_blocks_enable_without_telegram_probe(
    monkeypatch,
) -> None:
    before = datetime.utcnow() - timedelta(hours=25)
    account = _stale_eligible_account()
    membership = SimpleNamespace(
        resource_type="user",
        resource_id=account.id,
        status="member_verified",
        last_verified_at=before,
        telegram_user_id=None,
        updated_at=before,
    )
    db = AsyncMock()
    db.scalar.side_effect = [account, membership, None]
    probe = AsyncMock(
        return_value=MembershipProbeResult(
            verified=True,
            telegram_user_id=8008,
        )
    )
    service = OwnedGroupMessagingPolicyService(db)
    service.target_resolver.membership_probe = probe
    service.target_resolver.resolve = AsyncMock(return_value=object())
    monkeypatch.setattr(settings, "OWNED_GROUP_MESSAGING_ENABLED", True)

    with pytest.raises(OwnedGroupMessagingError) as exc_info:
        await service._validate_enabling(901, account.id, enabled=True)

    probe.assert_not_awaited()
    assert "telegram_identity_unbound" in exc_info.value.details["blocking_reasons"]
    assert membership.status == "unknown_needs_reconcile"
    assert membership.telegram_user_id is None
    assert membership.last_verified_at == before
    db.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_stale_membership_probe_blocks_enable(monkeypatch) -> None:
    before = datetime.utcnow() - timedelta(hours=25)
    account = _stale_eligible_account()
    membership = SimpleNamespace(
        resource_type="user",
        resource_id=account.id,
        status="member_verified",
        last_verified_at=before,
        telegram_user_id=7007,
        updated_at=before,
    )
    asset = SimpleNamespace(id=901, telegram_chat_id=-1000000000901)
    db = AsyncMock()
    db.scalar.side_effect = [account, membership, None]
    db.get.return_value = asset
    probe = AsyncMock(
        return_value=MembershipProbeResult(
            verified=False,
            reason_code="membership_probe_failed",
        )
    )
    service = OwnedGroupMessagingPolicyService(db)
    service.target_resolver.membership_probe = probe
    service.target_resolver.resolve = AsyncMock(return_value=object())
    monkeypatch.setattr(settings, "OWNED_GROUP_MESSAGING_ENABLED", True)

    with pytest.raises(OwnedGroupMessagingError) as exc_info:
        await service._validate_enabling(asset.id, account.id, enabled=True)

    probe.assert_awaited_once_with(asset=asset, account=account)
    assert exc_info.value.code == "ACCOUNT_NOT_ELIGIBLE"
    assert "membership_probe_failed" in exc_info.value.details["blocking_reasons"]
    assert "membership_verification_stale" in exc_info.value.details["blocking_reasons"]
    assert membership.last_verified_at == before
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_probe_rejects_telegram_identity_mismatch(monkeypatch) -> None:
    before = datetime.utcnow() - timedelta(hours=25)
    account = _stale_eligible_account()
    membership = SimpleNamespace(
        resource_type="user",
        resource_id=account.id,
        status="member_verified",
        last_verified_at=before,
        telegram_user_id=7007,
        updated_at=before,
    )
    asset = SimpleNamespace(id=901, telegram_chat_id=-1000000000901)
    db = AsyncMock()
    db.scalar.side_effect = [account, membership, None]
    db.get.return_value = asset
    probe = AsyncMock(
        return_value=MembershipProbeResult(
            verified=True,
            telegram_user_id=8008,
        )
    )
    service = OwnedGroupMessagingPolicyService(db)
    service.target_resolver.membership_probe = probe
    service.target_resolver.resolve = AsyncMock(return_value=object())
    monkeypatch.setattr(settings, "OWNED_GROUP_MESSAGING_ENABLED", True)

    with pytest.raises(OwnedGroupMessagingError) as exc_info:
        await service._validate_enabling(asset.id, account.id, enabled=True)

    assert "telegram_identity_mismatch" in exc_info.value.details["blocking_reasons"]
    assert membership.status == "unknown_needs_reconcile"
    assert membership.telegram_user_id == 7007
    assert membership.last_verified_at == before
    db.flush.assert_awaited_once()


@pytest.mark.parametrize(
    ("body", "expected_code"),
    [
        (
            OwnedGroupTemplateWrite(
                name="filter",
                content="hello {{group_name|upper}}",
                message_type="interaction",
                template_variables=["group_name"],
            ),
            "INVALID_TEMPLATE_VARIABLE",
        ),
        (
            OwnedGroupTemplateWrite(
                name="community-promotion",
                content="hello {{promotion_url}}",
                message_type="qa",
                template_variables=["promotion_url"],
            ),
            "INVALID_TEMPLATE_VARIABLE",
        ),
        (
            OwnedGroupTemplateWrite(
                name="hard-coded-url",
                content="hello https://example.com/path",
                message_type="guide",
                template_variables=[],
            ),
            "CONTENT_SAFETY_BLOCKED",
        ),
        (
            OwnedGroupTemplateWrite(
                name="hard-coded-telegram-link",
                content="join t.me/example",
                message_type="guide",
                template_variables=[],
            ),
            "CONTENT_SAFETY_BLOCKED",
        ),
    ],
)
def test_template_save_rejects_expressions_promotion_leaks_and_static_urls(
    body: OwnedGroupTemplateWrite,
    expected_code: str,
) -> None:
    service = OwnedGroupMessagingPolicyService(AsyncMock())

    with pytest.raises(OwnedGroupMessagingError) as exc_info:
        service._validate_template_body(body)

    assert exc_info.value.code == expected_code


def test_template_schema_trims_name_and_rejects_blank_fields() -> None:
    body = OwnedGroupTemplateWrite(
        name="  welcome  ",
        content="hello {{group_name}}",
        message_type="interaction",
        template_variables=["group_name"],
    )
    assert body.name == "welcome"
    with pytest.raises(ValueError):
        OwnedGroupTemplateWrite(name="   ", content="hello", message_type="interaction")
    with pytest.raises(ValueError):
        OwnedGroupTemplateWrite(name="welcome", content="   ", message_type="interaction")


@pytest.mark.asyncio
async def test_scheduled_template_reference_rejects_user_name() -> None:
    template = SimpleNamespace(
        id=15,
        content="hello {{user_name}}",
        message_type="interaction",
        get_variables=lambda: ["user_name"],
    )
    service = OwnedGroupMessagingPolicyService(AsyncMock())
    service._owned_template = AsyncMock(return_value=template)
    service._validate_keyword_triggers = AsyncMock()
    request = PolicyCreate(
        account_id=7,
        mode="template",
        default_template_id=15,
        trigger_config={
            "scheduled": {
                "enabled": True,
                "times": ["10:30"],
                "content_category": "community",
            }
        },
    )

    with pytest.raises(OwnedGroupMessagingError) as exc_info:
        await service._validate_policy_references(901, request)

    assert exc_info.value.code == "INVALID_TEMPLATE_VARIABLE"
    assert exc_info.value.details["variable"] == "user_name"


@pytest.mark.asyncio
async def test_policy_unique_conflict_rolls_back_failed_session() -> None:
    db = AsyncMock()
    db.add = MagicMock()
    db.scalar.return_value = None
    db.flush.side_effect = IntegrityError("INSERT", {}, RuntimeError("duplicate"))
    service = OwnedGroupMessagingPolicyService(db)
    service._asset = AsyncMock(return_value=SimpleNamespace(id=901, core_group_id=801))
    service._require_account_eligible = AsyncMock()
    service._validate_enabling = AsyncMock()
    service._validate_policy_references = AsyncMock()

    with pytest.raises(OwnedGroupMessagingError) as exc_info:
        await service.create_policy(
            901,
            PolicyCreate(account_id=7),
            actor={"id": 1},
            correlation_id="policy-race",
        )

    assert exc_info.value.code == "POLICY_ALREADY_EXISTS"
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_stats_use_operating_day_and_runtime_cooldown(monkeypatch) -> None:
    day_start = datetime(2026, 9, 9, 16, 0, 0)
    monkeypatch.setattr(
        policy_service_module,
        "operating_day_start",
        lambda now=None: day_start,
    )
    runtime = {"minGroupCooldownSeconds": 600}
    monkeypatch.setattr(
        policy_service_module,
        "get_owned_group_messaging_settings",
        AsyncMock(return_value=runtime),
    )

    eligible_db = AsyncMock()
    counts = MagicMock()
    counts.one.return_value = (0, 0)
    eligible_db.execute.return_value = counts
    eligible_db.scalar.side_effect = [0, 0]
    eligible_service = OwnedGroupMessagingPolicyService(eligible_db)
    eligible_service._asset = AsyncMock(
        return_value=SimpleNamespace(
            id=901,
            core_group_id=801,
            telegram_chat_id=-1000000000901,
            governance_status="managed",
        )
    )
    eligible_service.target_resolver.list_account_eligibility = AsyncMock(return_value=[])
    await eligible_service.eligible_accounts(901)
    sent_query = eligible_db.scalar.await_args_list[0].args[0]
    assert day_start in sent_query.compile().params.values()

    last_sent_at = datetime(2026, 9, 10, 1, 0, 0)
    policy_db = AsyncMock()
    policy_counts = MagicMock()
    policy_counts.one.return_value = (1, 1, 0, last_sent_at)
    policy_db.execute.return_value = policy_counts
    policy_db.scalar.side_effect = [1, 0]
    service = OwnedGroupMessagingPolicyService(policy_db)
    service.target_resolver.validate_account_eligibility = AsyncMock(
        return_value=SimpleNamespace(
            display_name="sender",
            eligible=True,
            blocking_reasons=(),
        )
    )
    policy = GroupAccountMessagePolicy(
        owned_group_asset_id=901,
        core_group_id=801,
        account_id=7,
        mode="ai",
        default_template_id=None,
        trigger_config={},
        promotion_config={},
        daily_limit=5,
        cooldown_seconds=60,
        allowed_topics=[],
        require_review=True,
        enabled=True,
        revision=1,
        created_at=last_sent_at,
        updated_at=last_sent_at,
    )
    policy.id = 15

    response = await service._policy_response(
        policy,
        persona_summary={
            "account_id": 7,
            "configured": False,
            "name": None,
            "revision": 0,
            "applicable": True,
            "effective_enabled": False,
        },
    )

    assert response["cooldown_until"] == (last_sent_at + timedelta(seconds=600)).isoformat() + "Z"
    policy_query = policy_db.execute.await_args.args[0]
    assert day_start in policy_query.compile().params.values()


@pytest.mark.asyncio
async def test_approval_work_runs_inside_chat_advisory_lock(monkeypatch) -> None:
    db = AsyncMock()
    db.scalar.return_value = -1000000000901
    service = OwnedGroupMessagingPolicyService(db)
    events: list[str] = []

    @asynccontextmanager
    async def fake_lock(lock_db, telegram_chat_id):
        assert lock_db is db
        assert telegram_chat_id == -1000000000901
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    async def approve_locked(*args, **kwargs):
        assert events == ["enter"]
        events.append("approve")
        return {"id": 88}

    monkeypatch.setattr(policy_service_module, "telegram_chat_advisory_lock", fake_lock)
    service._approve_execution_locked = AsyncMock(side_effect=approve_locked)

    result = await service.approve_execution(
        901,
        88,
        ExecutionApprove(revision=1),
        actor={"id": 1},
        correlation_id="review-lock",
    )

    assert result == {"id": 88}
    assert events == ["enter", "approve", "exit"]


@pytest.mark.asyncio
async def test_non_admin_execution_response_never_contains_message_body(monkeypatch) -> None:
    monkeypatch.setattr(
        policy_service_module,
        "get_owned_group_messaging_settings",
        AsyncMock(return_value={"reviewTtlHours": 24}),
    )
    now = datetime.utcnow()
    execution = GroupAccountMessageExecution(
        policy_id=15,
        owned_group_asset_id=901,
        core_group_id=801,
        telegram_chat_id=-1000000000901,
        account_id=7,
        trigger_type="manual",
        message_purpose="template",
        content_category="community",
        mode_snapshot="template",
        policy_revision=1,
        status="queued",
        content="private message body",
        content_hash="a" * 64,
        idempotency_key="non-admin-redaction",
        correlation_id="redaction-test",
        attempt_count=0,
        revision=1,
        created_at=now,
        updated_at=now,
    )
    execution.id = 88

    response = await OwnedGroupMessagingPolicyService(AsyncMock())._execution_response(
        execution,
        include_content=True,
        include_sensitive=False,
    )

    assert response["content"] is None
    assert response["content_summary"] is None
    assert "private message body" not in str(response)


@pytest.mark.asyncio
async def test_admin_execution_response_summarizes_legacy_raw_prompt_context(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        policy_service_module,
        "get_owned_group_messaging_settings",
        AsyncMock(return_value={"reviewTtlHours": 24}),
    )
    now = datetime.utcnow()
    execution = GroupAccountMessageExecution(
        policy_id=15,
        owned_group_asset_id=901,
        core_group_id=801,
        telegram_chat_id=-1000000000901,
        account_id=7,
        trigger_type="reply",
        message_purpose="community_ai",
        content_category="community",
        mode_snapshot="ai",
        policy_revision=1,
        status="queued",
        prompt_context={
            "source_text": "CANARY_MEMBER_MESSAGE",
            "user_name": "CANARY_MEMBER_NAME",
            "recent_context": [{"text": "CANARY_RECENT_CONTEXT"}],
            "variables": {"group_name": "CANARY_GROUP_NAME"},
            "request_fingerprint": "fingerprint-legacy",
        },
        idempotency_key="legacy-admin-redaction",
        correlation_id="legacy-redaction-test",
        attempt_count=0,
        revision=1,
        created_at=now,
        updated_at=now,
    )
    execution.id = 89
    db = AsyncMock()
    db.scalars.return_value = SimpleNamespace(all=lambda: [])

    response = await OwnedGroupMessagingPolicyService(db)._execution_response(
        execution,
        include_content=True,
        include_sensitive=True,
    )

    serialized = json.dumps(response, ensure_ascii=False)
    assert response["prompt_context"]["request_fingerprint"] == "fingerprint-legacy"
    assert response["prompt_context"]["source_message_present"] is True
    assert response["prompt_context"]["recent_context_message_count"] == 1
    for canary in (
        "CANARY_MEMBER_MESSAGE",
        "CANARY_MEMBER_NAME",
        "CANARY_RECENT_CONTEXT",
        "CANARY_GROUP_NAME",
    ):
        assert canary not in serialized


@pytest.mark.asyncio
async def test_template_engine_cache_and_queries_are_scope_partitioned(test_db) -> None:
    acquisition = MessageTemplate(
        name="growth",
        content="hello {{user_name}}",
        template_variables="user_name",
        message_type=MessageType.INTERACTION,
        enabled=True,
        scope="acquisition",
        owned_group_asset_id=None,
    )
    owned = MessageTemplate(
        name="owned",
        content="hello {{group_name}}",
        template_variables="group_name",
        message_type=MessageType.INTERACTION,
        enabled=True,
        scope="owned_group",
        owned_group_asset_id=901,
    )
    test_db.add_all([acquisition, owned])
    await test_db.flush()

    growth_engine = TemplateEngine(test_db)
    owned_engine = TemplateEngine(
        test_db,
        scope="owned_group",
        owned_group_asset_id=901,
    )
    assert await growth_engine.load_templates() == 1
    assert await owned_engine.load_templates() == 1
    assert await growth_engine.get_template(acquisition.id) is acquisition
    assert await growth_engine.get_template(owned.id) is None
    assert await owned_engine.get_template(owned.id) is owned
    assert await owned_engine.get_template(acquisition.id) is None

    acquisition_response = await list_message_templates(
        message_type=None,
        enabled=None,
        include_inline=True,
        db=test_db,
    )
    assert [item["id"] for item in acquisition_response["data"]] == [acquisition.id]
    with pytest.raises(HTTPException) as exc_info:
        await update_message_template(
            owned.id,
            MessageTemplateUpdate(enabled=False),
            test_db,
        )
    assert exc_info.value.status_code == 404
    with pytest.raises(OwnedGroupMessagingError) as owned_lookup:
        await OwnedGroupMessagingPolicyService(test_db)._owned_template(
            901,
            acquisition.id,
        )
    assert getattr(owned_lookup.value, "code", None) == "OWNED_GROUP_TEMPLATE_NOT_FOUND"


@pytest.mark.asyncio
async def test_non_admin_write_uses_stable_error_envelope() -> None:
    app = FastAPI()
    app.include_router(router, prefix="/api/owned-groups")

    async def viewer() -> dict:
        return {"id": 9, "role": "auditor"}

    async def fake_db():
        yield object()

    app.dependency_overrides[get_current_user] = viewer
    app.dependency_overrides[get_db] = fake_db
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/owned-groups/1/messages/policies",
            json={"account_id": 2},
            headers={"X-Correlation-ID": "test-rbac-1"},
        )

    assert response.status_code == 403
    assert response.json() == {
        "error": {
            "code": "ADMIN_REQUIRED",
            "message": "该写操作需要管理员权限",
            "details": {},
            "retryable": False,
        },
        "correlation_id": "test-rbac-1",
    }
    assert response.headers["X-Correlation-ID"] == "test-rbac-1"


@pytest.mark.asyncio
async def test_unhandled_failure_uses_non_leaking_error_envelope(monkeypatch) -> None:
    app = FastAPI()
    app.include_router(router, prefix="/api/owned-groups")

    async def fake_db():
        yield object()

    monkeypatch.setattr(
        OwnedGroupMessagingPolicyService,
        "eligible_accounts",
        AsyncMock(side_effect=RuntimeError("secret backend detail")),
    )
    app.dependency_overrides[get_db] = fake_db
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/api/owned-groups/1/messages/eligible-accounts",
            headers={"X-Correlation-ID": "test-500-1"},
        )

    assert response.status_code == 500
    payload = response.json()
    assert payload["error"]["code"] == "INTERNAL_ERROR"
    assert payload["error"]["retryable"] is True
    assert "secret backend detail" not in response.text
    assert payload["correlation_id"] == "test-500-1"


def test_migration_heads_and_curated_sql_chain() -> None:
    backend = Path(__file__).resolve().parents[4]
    alembic = (backend / "migrations/versions/034_add_owned_group_messaging.py").read_text(
        encoding="utf-8"
    )
    sql = (backend / "migrations/048_add_owned_group_messaging.sql").read_text(encoding="utf-8")
    runner = (backend / "scripts/apply_sql_migrations.py").read_text(encoding="utf-8")
    assert 'revision = "034_owned_group_messaging"' in alembic
    assert 'down_revision = "033_owned_group_governance"' in alembic
    assert "group_account_message_policy" in sql
    assert "group_account_message_execution" in sql
    assert '"048_add_owned_group_messaging.sql"' in runner
    assert runner.index('"047_add_owned_group_governance.sql"') < runner.index(
        '"048_add_owned_group_messaging.sql"'
    )
    downgrade = alembic[alembic.index("def downgrade()") :]
    assert downgrade.index(
        '"fk_acquisition_message_owned_group_execution_id_group_account_m"'
    ) < downgrade.index('op.drop_table("group_account_message_execution")')
    assert downgrade.index('op.drop_table("group_account_message_execution")') < downgrade.index(
        'op.drop_table("group_account_message_policy")'
    )
