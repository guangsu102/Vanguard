from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event

from app.api.accounts import router
from app.core.account.models import (
    AccountOperationConfig,
    AccountOperationMode,
    AccountType,
    TelegramAccount,
)
from app.core.account.persona import hash_persona
from app.core.database import get_db
from app.modules.owned_group.models import OwnedGroupAsset


def _persona(name: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "name": name,
        "tone": "自然、克制、简短",
        "interests": ["网络稳定性"],
        "expertise": ["技术排障"],
        "reply_length": "short",
        "preferred_topics": ["使用体验"],
        "forbidden_topics": ["过度营销"],
        "ad_style": "soft_share",
        "catchphrases": [],
        "language_style": "zh_cn",
        "system_prompt": "",
    }


@asynccontextmanager
async def _client(test_db):
    app = FastAPI()
    app.include_router(router, prefix="/api/accounts")

    async def database_override():
        yield test_db

    app.dependency_overrides[get_db] = database_override
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client


async def _add_account(
    test_db,
    suffix: str,
    *,
    account_type: AccountType = AccountType.PROMOTER,
    operation_mode: str | None = AccountOperationMode.GROWTH.value,
    persona: dict[str, object] | None = None,
    revision: int = 0,
    persona_hash: str | None = None,
) -> TelegramAccount:
    account = TelegramAccount(
        identifier=f"persona-summary-{suffix}",
        session_name=f"persona-summary-{suffix}",
        account_type=account_type,
        ai_persona=persona,
        ai_persona_revision=revision,
        ai_persona_hash=(
            persona_hash
            if persona_hash is not None
            else hash_persona(persona)
            if persona is not None
            else None
        ),
    )
    test_db.add(account)
    await test_db.flush()
    if operation_mode is not None:
        test_db.add(
            AccountOperationConfig(
                account_id=account.id,
                operation_mode=operation_mode,
            )
        )
    return account


@pytest.mark.asyncio
async def test_account_list_returns_safe_persona_summaries_without_n_plus_one(test_db):
    valid = await _add_account(
        test_db,
        "valid",
        persona=_persona("技术型群友"),
        revision=3,
    )
    neutral = await _add_account(test_db, "neutral")
    ad_only = await _add_account(
        test_db,
        "ad-only",
        operation_mode=AccountOperationMode.AD_ONLY.value,
        persona=_persona("广告账号旧配置"),
        revision=2,
    )
    missing_config = await _add_account(
        test_db,
        "missing-config",
        operation_mode=None,
        persona=_persona("缺配置"),
        revision=1,
    )
    guardian = await _add_account(
        test_db,
        "guardian",
        account_type=AccountType.GUARDIAN_BOT,
        persona=_persona("守护机器人"),
        revision=1,
    )
    corrupt = await _add_account(
        test_db,
        "corrupt",
        persona={"schema_version": 1, "name": "do-not-leak"},
        revision=4,
        persona_hash="a" * 64,
    )
    corrupt_hash = await _add_account(
        test_db,
        "corrupt-hash",
        persona=_persona("hash-must-not-leak"),
        revision=5,
        persona_hash="f" * 64,
    )
    test_db.add_all(
        [
            OwnedGroupAsset(
                internal_name="persona-summary-created-1",
                telegram_chat_id=-100000000001,
                title="已创建群 1",
                visibility="private",
                owner_account_id=valid.id,
                invite_mode="direct_invite",
                status="ready",
            ),
            OwnedGroupAsset(
                internal_name="persona-summary-created-2",
                telegram_chat_id=-100000000002,
                title="已创建群 2",
                visibility="private",
                owner_account_id=valid.id,
                invite_mode="direct_invite",
                status="needs_attention",
            ),
            OwnedGroupAsset(
                internal_name="persona-summary-local-draft",
                title="本地草稿",
                visibility="private",
                owner_account_id=valid.id,
                invite_mode="direct_invite",
                status="draft",
            ),
        ]
    )
    await test_db.commit()

    statements: list[str] = []

    def record_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(test_db.bind.sync_engine, "before_cursor_execute", record_statement)
    try:
        async with _client(test_db) as client:
            response = await client.get("/api/accounts", params={"limit": 100})
    finally:
        event.remove(test_db.bind.sync_engine, "before_cursor_execute", record_statement)

    assert response.status_code == 200
    by_id = {item["id"]: item for item in response.json()["data"]}
    assert {
        key: by_id[valid.id][key]
        for key in (
            "persona_configured",
            "persona_name",
            "persona_revision",
            "persona_applicable",
        )
    } == {
        "persona_configured": True,
        "persona_name": "技术型群友",
        "persona_revision": 3,
        "persona_applicable": True,
    }
    assert by_id[neutral.id]["persona_configured"] is False
    assert by_id[neutral.id]["persona_name"] is None
    assert by_id[neutral.id]["persona_revision"] == 0
    assert by_id[neutral.id]["persona_applicable"] is True
    assert by_id[valid.id]["owned_group_created_count"] == 2
    assert by_id[neutral.id]["owned_group_created_count"] == 0
    assert by_id[ad_only.id]["persona_applicable"] is False
    assert by_id[missing_config.id]["persona_applicable"] is False
    assert by_id[guardian.id]["persona_applicable"] is False
    assert by_id[corrupt.id]["persona_configured"] is True
    assert by_id[corrupt.id]["persona_name"] is None
    assert by_id[corrupt.id]["persona_revision"] == 4
    assert by_id[corrupt.id]["persona_applicable"] is False
    assert by_id[corrupt_hash.id]["persona_configured"] is True
    assert by_id[corrupt_hash.id]["persona_name"] is None
    assert by_id[corrupt_hash.id]["persona_revision"] == 5
    assert by_id[corrupt_hash.id]["persona_applicable"] is False
    assert "do-not-leak" not in response.text
    assert "hash-must-not-leak" not in response.text

    # One total count query, one account query, and one bulk owned-group count;
    # Persona, operation config, and owned-group counts are not selected once
    # per account.
    assert len(statements) == 3
    assert "ai_persona" in statements[1]
    assert "telegram_account_operation_config" in statements[1]
    assert "owned_group_assets" in statements[2].lower()


@pytest.mark.asyncio
async def test_generic_account_writes_reject_all_persona_columns_with_422(test_db):
    forbidden_values = {
        "ai_persona": _persona("禁止旁路"),
        "ai_persona_revision": 7,
        "ai_persona_hash": "b" * 64,
        "ai_persona_updated_at": "2026-09-10T00:00:00Z",
        "ai_persona_updated_by": 42,
    }

    async with _client(test_db) as client:
        for field, value in forbidden_values.items():
            responses = (
                await client.post("/api/accounts", json={"identifier": "blocked", field: value}),
                await client.put("/api/accounts/999999", json={field: value}),
                await client.post(
                    "/api/accounts/batch-import",
                    json={"accounts": [{"identifier": "blocked-batch", field: value}]},
                ),
            )
            for response in responses:
                assert response.status_code == 422
                errors = response.json()["detail"]
                assert any(
                    error["type"] == "extra_forbidden" and field in error["loc"]
                    for error in errors
                )
