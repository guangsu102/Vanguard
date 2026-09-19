from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.core.account.bot_credentials import resolve_guardian_bot_token
from app.core.account.models import (
    AccountStatus,
    AccountType,
    GuardianBotProfile,
    ManagedBotProvision,
    ManagedBotProvisionStatus,
    ManagedBotProvisionStep,
    TelegramAccount,
)
from app.core.account.telegram_execution import TelegramExecutionError
from app.core.ephemeral_secret import decrypt_ephemeral_secret
from app.integrations.telegram.client import TelegramAPIError
from app.modules.managed_bot_provision import service as service_module
from app.modules.managed_bot_provision.service import (
    ManagedBotProvisionLeaseLost,
    ManagedBotProvisionService,
    create_managed_bot_provision,
)
from app.modules.owned_group.models_extra import OwnedBotProfile

MANAGER_TOKEN = "500000:manager-secret"
CHILD_TOKEN = "900001:created-managed-secret"
BOT_USER_ID = 900001


class FakeUserClient:
    def __init__(self, recovered_entity=None, get_entity_error: Exception | None = None):
        self.recovered_entity = recovered_entity
        self.get_entity_error = get_entity_error
        self.get_entity_calls: list[str] = []

    async def get_entity(self, username: str):
        self.get_entity_calls.append(username)
        if self.get_entity_error is not None:
            raise self.get_entity_error
        if self.recovered_entity is None:
            raise AssertionError("unexpected recovery lookup")
        return self.recovered_entity


class FakeAccountPool:
    def __init__(self, *, recovered_entity=None, get_entity_error=None):
        self.wrapper = SimpleNamespace(
            client=FakeUserClient(recovered_entity, get_entity_error)
        )
        self.added_account_ids: list[int] = []
        self.acquired_account_ids: list[int] = []
        self.released = 0

    async def add_account_from_db(self, account):
        self.added_account_ids.append(account.id)
        return self.wrapper

    async def acquire_by_id(self, account_id: int, **_kwargs):
        self.acquired_account_ids.append(account_id)
        return self.wrapper

    async def release(self, wrapper):
        assert wrapper is self.wrapper
        self.released += 1


class FakeTelegramExecution:
    def __init__(
        self,
        *,
        username_available: bool = True,
        username: str,
        check_error: Exception | None = None,
    ):
        self.username_available = username_available
        self.username = username
        self.check_error = check_error
        self.check_calls: list[str] = []
        self.create_calls: list[dict] = []

    async def check_managed_bot_username(self, wrapper, username: str) -> bool:
        assert wrapper is not None
        self.check_calls.append(username)
        if self.check_error is not None:
            raise self.check_error
        return self.username_available

    async def create_managed_bot(self, wrapper, **kwargs):
        assert wrapper is not None
        self.create_calls.append(kwargs)
        return SimpleNamespace(id=BOT_USER_ID, bot=True, username=self.username)


def _install_fake_bot_api(
    monkeypatch,
    *,
    username: str,
    managed_token_error: TelegramAPIError | None = None,
    manager_can_manage: bool = True,
):
    class FakeTelegramClient:
        constructed_tokens: list[str] = []
        managed_token_requests: list[int] = []
        close_calls = 0

        def __init__(self, config):
            self.token = config.bot_token
            self.__class__.constructed_tokens.append(self.token)

        async def get_me(self):
            if self.token == MANAGER_TOKEN:
                return SimpleNamespace(
                    user_id=500000,
                    username="manager_bot",
                    first_name="Manager",
                    full_name="Manager",
                    is_bot=True,
                    can_manage_bots=manager_can_manage,
                )
            assert self.token == CHILD_TOKEN
            return SimpleNamespace(
                user_id=BOT_USER_ID,
                username=username,
                first_name="Created",
                full_name="Created Managed Bot",
                is_bot=True,
                can_manage_bots=False,
            )

        async def get_managed_bot_token(self, user_id: int) -> str:
            assert self.token == MANAGER_TOKEN
            self.__class__.managed_token_requests.append(user_id)
            if managed_token_error is not None:
                raise managed_token_error
            return CHILD_TOKEN

        async def close(self):
            self.__class__.close_calls += 1

    monkeypatch.setattr(service_module, "TelegramClient", FakeTelegramClient)
    return FakeTelegramClient


async def _seed_owner_and_manager(test_db, *, suffix: str):
    owner = TelegramAccount(
        identifier=f"+1555{suffix}",
        phone=f"+1555{suffix}",
        session_name=f"managed-owner-{suffix}",
        session_string="owner-session",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    manager_account = TelegramAccount(
        identifier=f"@manager_{suffix}_bot",
        session_name=f"manager-{suffix}",
        account_type=AccountType.GUARDIAN_BOT,
        status=AccountStatus.IDLE,
        is_active=True,
    )
    test_db.add_all([owner, manager_account])
    await test_db.flush()
    manager = GuardianBotProfile(
        account_id=manager_account.id,
        bot_token=MANAGER_TOKEN,
        bot_username="manager_bot",
        bot_user_id=500000,
        enabled=True,
    )
    test_db.add(manager)
    await test_db.commit()
    return owner, manager


async def _create_operation(
    test_db,
    owner: TelegramAccount,
    manager: GuardianBotProfile,
    *,
    username: str,
    key: str,
):
    return await create_managed_bot_provision(
        test_db,
        owner_account_id=owner.id,
        manager_bot_profile_id=manager.id,
        display_name="Managed Service Test",
        username=username,
        idempotency_key=key,
        created_by_id=7001,
    )


@pytest.mark.asyncio
async def test_create_request_reuses_idempotency_key_and_rejects_conflict(test_db):
    owner, manager = await _seed_owner_and_manager(test_db, suffix="1001")
    kwargs = {
        "owner_account_id": owner.id,
        "manager_bot_profile_id": manager.id,
        "display_name": "Idempotent Bot",
        "username": "idempotent_test_bot",
        "idempotency_key": "idem-key-1001",
        "created_by_id": 7001,
    }

    first, first_created = await create_managed_bot_provision(test_db, **kwargs)
    second, second_created = await create_managed_bot_provision(test_db, **kwargs)

    assert first_created is True
    assert second_created is False
    assert second.id == first.id
    assert await test_db.scalar(select(func.count(ManagedBotProvision.id))) == 1

    with pytest.raises(ValueError, match="IDEMPOTENCY_KEY_CONFLICT"):
        await create_managed_bot_provision(
            test_db,
            **{**kwargs, "display_name": "Different Request"},
        )


@pytest.mark.asyncio
async def test_username_unavailable_fails_without_create(test_db, monkeypatch):
    username = "occupied_service_bot"
    owner, manager = await _seed_owner_and_manager(test_db, suffix="1002")
    operation, _ = await _create_operation(
        test_db,
        owner,
        manager,
        username=username,
        key="idem-key-1002",
    )
    bot_api = _install_fake_bot_api(monkeypatch, username=username)
    pool = FakeAccountPool()
    execution = FakeTelegramExecution(username_available=False, username=username)

    result = await ManagedBotProvisionService(
        test_db,
        account_pool=pool,
        telegram_execution=execution,
    ).run_tick(limit=1)

    await test_db.refresh(operation)
    assert result == {"processed": 1, "recovered": 0}
    assert operation.status == ManagedBotProvisionStatus.FAILED.value
    assert operation.current_step == ManagedBotProvisionStep.CHECK_USERNAME.value
    assert operation.error_code == "USERNAME_OCCUPIED"
    assert operation.retryable is False
    assert execution.check_calls == [username]
    assert execution.create_calls == []
    assert bot_api.managed_token_requests == []
    assert pool.released == 1


@pytest.mark.asyncio
async def test_success_creates_once_encrypts_profiles_and_completes(
    test_db,
    monkeypatch,
):
    username = "successful_service_bot"
    owner, manager = await _seed_owner_and_manager(test_db, suffix="1003")
    operation, _ = await _create_operation(
        test_db,
        owner,
        manager,
        username=username,
        key="idem-key-1003",
    )
    bot_api = _install_fake_bot_api(monkeypatch, username=username)
    pool = FakeAccountPool()
    execution = FakeTelegramExecution(username_available=True, username=username)

    await ManagedBotProvisionService(
        test_db,
        account_pool=pool,
        telegram_execution=execution,
    ).run_tick(limit=1)

    await test_db.refresh(operation)
    assert operation.status == ManagedBotProvisionStatus.SUCCEEDED.value
    assert operation.current_step == ManagedBotProvisionStep.COMPLETE.value
    assert operation.bot_user_id == BOT_USER_ID
    assert operation.guardian_bot_profile_id is not None
    assert operation.owned_bot_profile_id is not None
    assert len(execution.create_calls) == 1
    assert execution.create_calls[0]["username"] == username
    assert execution.create_calls[0]["risk_reservation_id"] == (
        f"managed-bot-{operation.id}-{operation.request_hash[:16]}"
    )
    assert bot_api.managed_token_requests == [BOT_USER_ID]

    guardian = await test_db.get(
        GuardianBotProfile, operation.guardian_bot_profile_id
    )
    owned = await test_db.get(OwnedBotProfile, operation.owned_bot_profile_id)
    assert guardian is not None
    assert owned is not None
    assert guardian.bot_token.startswith("vge1:")
    assert owned.token_ciphertext.startswith("vge1:")
    assert CHILD_TOKEN not in guardian.bot_token
    assert CHILD_TOKEN not in owned.token_ciphertext
    assert resolve_guardian_bot_token(guardian.bot_token) == CHILD_TOKEN
    assert decrypt_ephemeral_secret(owned.token_ciphertext) == CHILD_TOKEN
    assert owned.status == "verified"
    assert pool.released == 1


@pytest.mark.asyncio
async def test_retry_after_create_attempt_existing_bot_requires_attention_without_token(
    test_db,
    monkeypatch,
):
    username = "recovered_service_bot"
    owner, manager = await _seed_owner_and_manager(test_db, suffix="1004")
    operation, _ = await _create_operation(
        test_db,
        owner,
        manager,
        username=username,
        key="idem-key-1004",
    )
    operation.create_attempted_at = datetime.utcnow()
    await test_db.commit()

    recovered = SimpleNamespace(id=BOT_USER_ID, bot=True, username=username)
    bot_api = _install_fake_bot_api(monkeypatch, username=username)
    pool = FakeAccountPool(recovered_entity=recovered)
    execution = FakeTelegramExecution(username_available=True, username=username)

    await ManagedBotProvisionService(
        test_db,
        account_pool=pool,
        telegram_execution=execution,
    ).run_tick(limit=1)

    await test_db.refresh(operation)
    assert operation.status == ManagedBotProvisionStatus.NEEDS_ATTENTION.value
    assert operation.current_step == ManagedBotProvisionStep.FETCH_TOKEN.value
    assert operation.error_code == "TELEGRAM_CREATE_OUTCOME_UNKNOWN"
    assert operation.retryable is False
    assert operation.bot_user_id is None
    assert operation.guardian_bot_profile_id is None
    assert operation.owned_bot_profile_id is None
    assert pool.wrapper.client.get_entity_calls == [username]
    assert pool.released == 1
    assert execution.check_calls == []
    assert execution.create_calls == []
    assert bot_api.managed_token_requests == []
    assert bot_api.constructed_tokens == [MANAGER_TOKEN]


@pytest.mark.asyncio
async def test_create_attempt_value_error_for_missing_username_can_create(
    test_db,
    monkeypatch,
):
    username = "missing_lookup_service_bot"
    owner, manager = await _seed_owner_and_manager(test_db, suffix="1009")
    operation, _ = await _create_operation(
        test_db,
        owner,
        manager,
        username=username,
        key="idem-key-1009",
    )
    operation.create_attempted_at = datetime.utcnow()
    await test_db.commit()

    bot_api = _install_fake_bot_api(monkeypatch, username=username)
    pool = FakeAccountPool(
        get_entity_error=ValueError(
            f"No user has {username!r} as username"
        )
    )
    execution = FakeTelegramExecution(username_available=True, username=username)

    await ManagedBotProvisionService(
        test_db,
        account_pool=pool,
        telegram_execution=execution,
    ).run_tick(limit=1)

    await test_db.refresh(operation)
    assert operation.status == ManagedBotProvisionStatus.SUCCEEDED.value
    assert operation.current_step == ManagedBotProvisionStep.COMPLETE.value
    assert operation.bot_user_id == BOT_USER_ID
    assert pool.wrapper.client.get_entity_calls == [username]
    assert pool.released == 1
    assert execution.check_calls == [username]
    assert len(execution.create_calls) == 1
    assert bot_api.managed_token_requests == [BOT_USER_ID]


@pytest.mark.parametrize(
    ("telegram_error_name", "expected_code"),
    [
        ("UsernameOccupiedError", "USERNAME_OCCUPIED"),
        ("UsernameInvalidError", "USERNAME_INVALID"),
        ("UsernamePurchaseAvailableError", "USERNAME_PURCHASE_AVAILABLE"),
    ],
)
@pytest.mark.asyncio
async def test_wrapped_username_errors_are_terminal_without_create(
    test_db,
    monkeypatch,
    telegram_error_name: str,
    expected_code: str,
):
    username = "terminal_check_service_bot"
    owner, manager = await _seed_owner_and_manager(test_db, suffix="1005")
    operation, _ = await _create_operation(
        test_db,
        owner,
        manager,
        username=username,
        key=f"idem-key-{telegram_error_name}",
    )
    _install_fake_bot_api(monkeypatch, username=username)
    pool = FakeAccountPool()
    execution = FakeTelegramExecution(
        username=username,
        check_error=TelegramExecutionError(
            f"managed_bot_username_check_failed:{telegram_error_name}"
        ),
    )

    await ManagedBotProvisionService(
        test_db,
        account_pool=pool,
        telegram_execution=execution,
    ).run_tick(limit=1)

    await test_db.refresh(operation)
    assert operation.status == ManagedBotProvisionStatus.FAILED.value
    assert operation.current_step == ManagedBotProvisionStep.CHECK_USERNAME.value
    assert operation.error_code == expected_code
    assert operation.retryable is False
    assert execution.check_calls == [username]
    assert execution.create_calls == []


@pytest.mark.asyncio
async def test_known_bot_id_recovers_without_owner_session_or_user_lookup(
    test_db,
    monkeypatch,
):
    username = "known_id_service_bot"
    owner, manager = await _seed_owner_and_manager(test_db, suffix="1006")
    operation, _ = await _create_operation(
        test_db,
        owner,
        manager,
        username=username,
        key="idem-key-1006",
    )
    operation.bot_user_id = BOT_USER_ID
    operation.create_attempted_at = datetime.utcnow()
    operation.external_created_at = datetime.utcnow()
    owner.status = AccountStatus.BANNED
    owner.session_string = None
    await test_db.commit()

    bot_api = _install_fake_bot_api(monkeypatch, username=username)
    pool = FakeAccountPool()
    execution = FakeTelegramExecution(username=username)

    await ManagedBotProvisionService(
        test_db,
        account_pool=pool,
        telegram_execution=execution,
    ).run_tick(limit=1)

    await test_db.refresh(operation)
    assert operation.status == ManagedBotProvisionStatus.SUCCEEDED.value
    assert operation.current_step == ManagedBotProvisionStep.COMPLETE.value
    assert pool.added_account_ids == []
    assert pool.acquired_account_ids == []
    assert pool.wrapper.client.get_entity_calls == []
    assert pool.released == 0
    assert execution.check_calls == []
    assert execution.create_calls == []
    assert bot_api.managed_token_requests == [BOT_USER_ID]
    assert bot_api.constructed_tokens == [MANAGER_TOKEN, CHILD_TOKEN]


@pytest.mark.parametrize(
    ("max_attempts", "expected_status", "expected_retryable"),
    [
        (3, ManagedBotProvisionStatus.RETRY_WAIT.value, True),
        (1, ManagedBotProvisionStatus.NEEDS_ATTENTION.value, False),
    ],
)
@pytest.mark.asyncio
async def test_known_bot_token_failure_never_recreates(
    test_db,
    monkeypatch,
    max_attempts: int,
    expected_status: str,
    expected_retryable: bool,
):
    username = "known_token_failure_bot"
    owner, manager = await _seed_owner_and_manager(test_db, suffix="1007")
    operation, _ = await _create_operation(
        test_db,
        owner,
        manager,
        username=username,
        key=f"idem-key-token-failure-{max_attempts}",
    )
    operation.bot_user_id = BOT_USER_ID
    operation.create_attempted_at = datetime.utcnow()
    operation.external_created_at = datetime.utcnow()
    operation.max_attempts = max_attempts
    owner.status = AccountStatus.BANNED
    owner.session_string = None
    await test_db.commit()

    bot_api = _install_fake_bot_api(
        monkeypatch,
        username=username,
        managed_token_error=TelegramAPIError(
            "Managed bot token request failed",
            code=503,
            method="getManagedBotToken",
        ),
    )
    pool = FakeAccountPool()
    execution = FakeTelegramExecution(username=username)

    await ManagedBotProvisionService(
        test_db,
        account_pool=pool,
        telegram_execution=execution,
    ).run_tick(limit=1)

    await test_db.refresh(operation)
    assert operation.status == expected_status
    assert operation.current_step == ManagedBotProvisionStep.FETCH_TOKEN.value
    assert operation.error_code == "MANAGED_BOT_TOKEN_UNAVAILABLE"
    assert operation.retryable is expected_retryable
    assert pool.added_account_ids == []
    assert pool.acquired_account_ids == []
    assert execution.check_calls == []
    assert execution.create_calls == []
    assert bot_api.managed_token_requests == [BOT_USER_ID]
    assert bot_api.constructed_tokens == [MANAGER_TOKEN]


@pytest.mark.asyncio
async def test_stale_lease_cannot_write_after_new_worker_claims_operation(test_db):
    username = "lease_fence_service_bot"
    owner, manager = await _seed_owner_and_manager(test_db, suffix="1008")
    operation, _ = await _create_operation(
        test_db,
        owner,
        manager,
        username=username,
        key="idem-key-1008",
    )
    service = ManagedBotProvisionService(
        test_db,
        account_pool=FakeAccountPool(),
        telegram_execution=FakeTelegramExecution(username=username),
    )
    claim = await service._claim_next(stale_after_seconds=300)
    assert claim is not None
    provision_id, stale_lease_id = claim

    claimed = await test_db.get(ManagedBotProvision, provision_id)
    assert claimed is not None
    claimed.lease_id = "replacement-worker-lease"
    await test_db.commit()

    with pytest.raises(ManagedBotProvisionLeaseLost):
        await service._set_step(
            claimed,
            stale_lease_id,
            ManagedBotProvisionStep.CREATE_BOT,
        )

    await test_db.refresh(operation)
    assert operation.lease_id == "replacement-worker-lease"
    assert operation.current_step == ManagedBotProvisionStep.PREFLIGHT.value


@pytest.mark.asyncio
async def test_failed_local_only_operation_releases_username_for_new_request(test_db):
    username = "released_failed_service_bot"
    owner, original_manager = await _seed_owner_and_manager(test_db, suffix="1010")
    _unused_owner, replacement_manager = await _seed_owner_and_manager(
        test_db,
        suffix="1011",
    )
    original, _ = await _create_operation(
        test_db,
        owner,
        original_manager,
        username=username,
        key="idem-key-1010-original",
    )
    original.status = ManagedBotProvisionStatus.FAILED.value
    original.create_attempted_at = datetime.utcnow()
    original.bot_user_id = None
    original.external_created_at = None
    await test_db.commit()

    replacement, created = await create_managed_bot_provision(
        test_db,
        owner_account_id=owner.id,
        manager_bot_profile_id=replacement_manager.id,
        display_name="Replacement Managed Bot",
        username=username,
        idempotency_key="idem-key-1010-replacement",
        created_by_id=7001,
    )

    assert created is True
    assert replacement.id != original.id
    assert replacement.manager_bot_profile_id == replacement_manager.id
    assert replacement.display_name == "Replacement Managed Bot"
    assert await test_db.scalar(select(func.count(ManagedBotProvision.id))) == 2


@pytest.mark.parametrize(
    ("reserved_status", "has_bot_user_id", "has_external_created_at"),
    [
        (ManagedBotProvisionStatus.QUEUED.value, False, False),
        (ManagedBotProvisionStatus.RUNNING.value, False, False),
        (ManagedBotProvisionStatus.RETRY_WAIT.value, False, False),
        (ManagedBotProvisionStatus.SUCCEEDED.value, False, False),
        (ManagedBotProvisionStatus.NEEDS_ATTENTION.value, False, False),
        (ManagedBotProvisionStatus.FAILED.value, True, False),
        (ManagedBotProvisionStatus.FAILED.value, False, True),
    ],
)
@pytest.mark.asyncio
async def test_reserved_or_externally_created_operation_keeps_username_conflict(
    test_db,
    reserved_status: str,
    has_bot_user_id: bool,
    has_external_created_at: bool,
):
    username = "reserved_conflict_service_bot"
    owner, original_manager = await _seed_owner_and_manager(test_db, suffix="1012")
    _unused_owner, replacement_manager = await _seed_owner_and_manager(
        test_db,
        suffix="1013",
    )
    original, _ = await _create_operation(
        test_db,
        owner,
        original_manager,
        username=username,
        key="idem-key-1012-original",
    )
    original.status = reserved_status
    original.bot_user_id = BOT_USER_ID if has_bot_user_id else None
    original.external_created_at = (
        datetime.utcnow() if has_external_created_at else None
    )
    await test_db.commit()

    with pytest.raises(ValueError, match="USERNAME_OPERATION_CONFLICT"):
        await create_managed_bot_provision(
            test_db,
            owner_account_id=owner.id,
            manager_bot_profile_id=replacement_manager.id,
            display_name="Conflicting Replacement",
            username=username,
            idempotency_key=(
                f"idem-conflict-{reserved_status}-{has_bot_user_id}-"
                f"{has_external_created_at}"
            ),
            created_by_id=7001,
        )

    assert await test_db.scalar(select(func.count(ManagedBotProvision.id))) == 1


class BotFatherStrategyExecution:
    """Stands in for the @BotFather conversation strategy on the owner session."""

    def __init__(self, username: str, token: str = CHILD_TOKEN):
        self.username = username
        self.token = token
        self.botfather_calls: list[dict] = []

    async def check_managed_bot_username(self, wrapper, username: str) -> bool:
        raise AssertionError("BotFather strategy must not pre-check usernames via MTProto")

    async def create_bot_via_botfather(
        self, wrapper, *, name: str, username: str, on_username_submitted=None, source="managed_bot_provision"
    ):
        assert wrapper is not None
        self.botfather_calls.append({"name": name, "username": username})
        if on_username_submitted is not None:
            await on_username_submitted()
        return self.token


@pytest.mark.asyncio
async def test_botfather_strategy_creates_bot_when_manager_lacks_manage_right(
    test_db,
    monkeypatch,
):
    username = "botfather_created_bot"
    owner, manager = await _seed_owner_and_manager(test_db, suffix="2001")
    bot_api = _install_fake_bot_api(
        monkeypatch, username=username, manager_can_manage=False
    )
    execution = BotFatherStrategyExecution(username=username)
    pool = FakeAccountPool()

    operation, _ = await _create_operation(
        test_db,
        owner,
        manager,
        username=username,
        key="idem-key-2001",
    )
    await ManagedBotProvisionService(
        test_db,
        account_pool=pool,
        telegram_execution=execution,
    ).run_tick(limit=1)

    await test_db.refresh(operation)
    assert operation.status == ManagedBotProvisionStatus.SUCCEEDED.value
    assert operation.current_step == ManagedBotProvisionStep.COMPLETE.value
    assert operation.bot_user_id == BOT_USER_ID
    assert execution.botfather_calls[0]["username"] == username
    assert bot_api.managed_token_requests == []

    guardian = await test_db.get(GuardianBotProfile, operation.guardian_bot_profile_id)
    owned = await test_db.get(OwnedBotProfile, operation.owned_bot_profile_id)
    assert guardian is not None and owned is not None
    assert resolve_guardian_bot_token(guardian.bot_token) == CHILD_TOKEN
    assert decrypt_ephemeral_secret(owned.token_ciphertext) == CHILD_TOKEN
    assert owned.status == "verified"
    assert owned.owner_account_id == owner.id
    assert pool.released == 1
