"""Safe Telethon adapter for self-owned promotional groups.

The owned-group worker deliberately knows nothing about Telegram.  This module
is the concrete side-effect boundary that can be injected into that worker.
It is disabled by default and refuses every mutating request unless the P0
execution gate, Redis-backed global stop, and strict resource precheck all
pass immediately before the request.

The adapter keeps the Telegram calls small and injectable.  Unit tests can
provide a fake :class:`AccountPool`, Telethon-like clients, and a safety
precheck without opening a network connection.  Production wiring should
construct it once per worker task and release the pool accounts in the same
task/loop that acquired them.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select

from app.core.account.models import AccountType, TelegramAccount
from app.core.account.operation_lease import (
    AccountOperationLeaseBusy,
    AccountOperationLeaseUnavailable,
)
from app.core.account.pool import get_account_pool
from app.core.account.risk_guard import AccountRiskAction
from app.core.account.telegram_execution import (
    TelegramExecutionError,
    TelegramExecutionService,
    TelegramJoinRequestPendingError,
)
from app.core.ephemeral_secret import decrypt_ephemeral_secret, encrypt_ephemeral_secret
from app.core.p0_safety_gate import (
    SafetyGateDecision,
    get_safety_gate_state,
    is_owned_group_execution_enabled,
    is_safety_gate_enabled,
    precheck_owned_group_resources,
)
from app.modules.owned_group.contracts import ItemStatus, ReasonCode, ResourceType
from app.modules.owned_group.models import (
    OwnedGroupAsset,
    OwnedGroupOperation,
    OwnedGroupOperationItem,
)
from app.modules.owned_group.models_extra import (
    OwnedBotProfile,
    OwnedGroupAdminAssignment,
    OwnedGroupInviteLink,
    OwnedGroupMembership,
)
from app.modules.owned_group.worker import (
    GroupCreateResult,
    ItemExecutionResult,
    PreflightResult,
)


class AdapterBlockedError(RuntimeError):
    """A P0 gate or a strict resource check denied a Telegram write."""

    def __init__(self, reason_code: str, message: str | None = None):
        self.reason_code = reason_code
        self.message = message or reason_code
        super().__init__(self.message)


class AdapterConfigurationError(RuntimeError):
    """The adapter cannot safely resolve a required account or bot."""

    def __init__(self, reason_code: str, message: str | None = None):
        self.reason_code = reason_code
        self.message = message or reason_code
        super().__init__(self.message)


class InviteLinkMutationError(RuntimeError):
    """A guarded invite-link mutation could not be completed safely."""

    def __init__(self, reason_code: str, message: str | None = None):
        self.reason_code = reason_code
        self.message = message or reason_code
        super().__init__(self.message)


@dataclass(frozen=True)
class TelegramErrorClassification:
    """Safe, transport-independent classification of a Telegram exception."""

    status: str
    reason_code: str
    transient: bool = False
    retry_after_seconds: int | None = None
    message: str = "Telegram operation failed"


class AccountPoolLike(Protocol):
    async def add_account_from_db(self, account: TelegramAccount) -> Any: ...

    async def acquire_by_id(
        self,
        account_id: int,
        purpose: str = "default",
        require_session: bool = True,
        raise_on_lease_failure: bool = False,
    ) -> Any: ...

    async def release(self, account: Any) -> None: ...


BotClientFactory = Callable[[str], Any]
SafetyPrecheck = Callable[..., Awaitable[SafetyGateDecision | Mapping[str, Any]]]
SafetyStateReader = Callable[[], Awaitable[Any]]
ExecutionEnabledReader = Callable[[], bool | Awaitable[bool]]


_SAFE_ERROR_REPLACEMENTS = (
    (re.compile(r"bot\d+:[A-Za-z0-9_-]+", re.IGNORECASE), "bot<redacted>"),
    (
        re.compile(r"https?://(?:www\.)?(?:t\.me|telegram\.me)/[^\s]+", re.IGNORECASE),
        "<invite-redacted>",
    ),
    (
        re.compile(r"(?:token|api[_-]?hash|session(?:_string)?)[=:\s]+[^\s,;]+", re.IGNORECASE),
        "credential=<redacted>",
    ),
)

_RETRYABLE_GATE_REASONS = {
    "global_stop_enabled",
    "safety_gate_backend_unavailable",
    "owned_group_execution_disabled",
}

_PRIVATE_INVITE_RE = re.compile(
    r"^(?:https?://(?:www\.)?(?:t\.me|telegram\.me)/(?:\+[A-Za-z0-9_-]{8,128}|"
    r"joinchat/[A-Za-z0-9_-]{8,128})|tg://join\?invite=[A-Za-z0-9_-]{8,128})"
    r"(?:\?[A-Za-z0-9_=&%-]*)?$",
    re.IGNORECASE,
)
_PUBLIC_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
_ADMIN_RIGHT_NAMES = (
    "change_info",
    "post_messages",
    "edit_messages",
    "delete_messages",
    "ban_users",
    "invite_users",
    "pin_messages",
    "add_admins",
    "anonymous",
    "manage_call",
    "other",
    "manage_topics",
    "post_stories",
    "edit_stories",
    "delete_stories",
)


def sanitize_telegram_error(value: Any, *, max_length: int = 240) -> str:
    """Return an operator-useful error without tokens, sessions, or invite URLs."""

    text = (
        f"{value.__class__.__name__}: {value}" if isinstance(value, BaseException) else str(value)
    )
    for pattern, replacement in _SAFE_ERROR_REPLACEMENTS:
        text = pattern.sub(replacement, text)
    text = " ".join(text.split())
    return text[:max_length] or "Telegram operation failed"


def _exception_name(exc: BaseException) -> str:
    return exc.__class__.__name__.lower()


def _flood_wait_seconds(exc: BaseException) -> int | None:
    value = getattr(exc, "seconds", None) or getattr(exc, "value", None)
    try:
        if value is not None:
            return max(1, int(value))
    except (TypeError, ValueError):
        pass
    match = re.search(r"wait of (\d+) seconds", str(exc), re.IGNORECASE)
    return max(1, int(match.group(1))) if match else None


def classify_telegram_error(
    exc: BaseException,
    *,
    mutating: bool = True,
    phase: str = "item",
) -> TelegramErrorClassification:
    """Map Telethon/Bot API errors to the owned-group state machine.

    Unknown exceptions from a mutating RPC intentionally become ``UNKNOWN``;
    retrying them blindly can duplicate an invite or an administrator change.
    """

    name = _exception_name(exc)
    safe_message = sanitize_telegram_error(exc)
    flood_seconds = _flood_wait_seconds(exc)
    if flood_seconds is not None or name in {"ratelimiterror", "toomanyrequests"}:
        return TelegramErrorClassification(
            status=ItemStatus.FAILED_TRANSIENT.value,
            reason_code=ReasonCode.FLOOD_WAIT.value,
            transient=True,
            retry_after_seconds=flood_seconds,
            message=safe_message,
        )
    if name in {"useralreadyparticipanterror", "alreadyparticipantserror"}:
        return TelegramErrorClassification(
            status=ItemStatus.SKIPPED_ALREADY_MEMBER.value,
            reason_code=ReasonCode.ALREADY_MEMBER.value,
            message=safe_message,
        )
    if name in {"inviterequestsenterror", "telegramjoinrequestpendingerror"}:
        return TelegramErrorClassification(
            status=ItemStatus.WAITING_APPROVAL.value,
            reason_code="join_request_pending",
            message=safe_message,
        )
    if name in {
        "userprivacyrestrictederror",
        "usernotmutualcontacterror",
        "userbannedinchannelerror",
        "userkickederror",
        "invitehashexpirederror",
        "invitehashinvaliderror",
        "channelprivateerror",
        "channelinvaliderror",
        "adminrankinvaliderror",
        "useradmininvaliderror",
        "rightforbiddenerror",
        "chatadminrequirederror",
        "usernameoccupiederror",
        "usernameinvaliderror",
        "usernamenotoccupiederror",
        "peerflooderror",
        "channelstoomucherror",
        "userstoomucherror",
    }:
        reason = ReasonCode.BANNED.value if "banned" in name or "kicked" in name else None
        if "privacy" in name:
            reason = ReasonCode.PRIVACY_RESTRICTED.value
        elif "mutual" in name:
            reason = ReasonCode.NOT_MUTUAL_CONTACT.value
        elif "admin" in name or "rightforbidden" in name:
            reason = (
                "admin_title_invalid" if "rank" in name else ReasonCode.NO_ADMIN_PERMISSION.value
            )
        elif "invitehash" in name:
            reason = ReasonCode.INVITE_EXPIRED.value
        elif "peerflood" in name:
            reason = ReasonCode.PEER_FLOOD.value
        elif "channelstoomuch" in name or "userstoomuch" in name:
            reason = "telegram_limit_reached"
        reason = reason or ("username_unavailable" if "username" in name else "telegram_rejected")
        return TelegramErrorClassification(
            status=ItemStatus.FAILED_PERMANENT.value,
            reason_code=reason,
            message=safe_message,
        )
    if name in {"telegramapierror", "ratelimiterror"}:
        code = getattr(exc, "code", None)
        if code in {401, 403}:
            return TelegramErrorClassification(
                status=ItemStatus.FAILED_PERMANENT.value,
                reason_code="bot_auth_failed",
                message="Bot API authentication or permission was rejected",
            )
        if code == 429:
            return TelegramErrorClassification(
                status=ItemStatus.FAILED_TRANSIENT.value,
                reason_code=ReasonCode.FLOOD_WAIT.value,
                transient=True,
                retry_after_seconds=_flood_wait_seconds(exc),
                message=safe_message,
            )

    if name in {
        "timeouterror",
        "asyncio.exceptions.timeouterror",
        "connectionerror",
        "connecterror",
        "servererror",
        "rpcerrorrepeated",
        "networkerror",
    } or isinstance(exc, (asyncio.TimeoutError, ConnectionError)):
        # Reads can safely retry; a write's outcome is unknown until reconcile.
        return TelegramErrorClassification(
            status=(
                ItemStatus.FAILED_TRANSIENT.value if not mutating else ItemStatus.UNKNOWN.value
            ),
            reason_code=(ReasonCode.NETWORK_TIMEOUT.value if mutating else "network_error"),
            transient=not mutating,
            message=safe_message,
        )
    if name in {"accountoperationleasebusy", "accountoperationleaseunavailable"}:
        return TelegramErrorClassification(
            status=ItemStatus.FAILED_TRANSIENT.value,
            reason_code=ReasonCode.ACCOUNT_COOLDOWN.value,
            transient=True,
            message=safe_message,
        )
    if isinstance(exc, TelegramExecutionError) and "risk_guard_blocked" in str(exc):
        return TelegramErrorClassification(
            status=ItemStatus.FAILED_PERMANENT.value,
            reason_code="risk_guard_blocked",
            message="Telegram operation blocked by account risk guard",
        )
    return TelegramErrorClassification(
        status=ItemStatus.UNKNOWN.value if mutating else ItemStatus.FAILED_TRANSIENT.value,
        reason_code=ReasonCode.UNKNOWN_NEEDS_RECONCILE.value if mutating else "telegram_error",
        transient=not mutating,
        message=safe_message,
    )


# A descriptive alias for callers that used the older naming in design notes.
classify_telegram_exception = classify_telegram_error


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _telegram_peer_id(entity: Any) -> int:
    """Get Telethon's canonical peer id, retaining the -100 prefix."""

    try:
        from telethon import utils

        value = int(utils.get_peer_id(entity))
        if value:
            return value
    except (TypeError, ValueError, AttributeError):
        pass
    value = getattr(entity, "id", None) or getattr(entity, "chat_id", None)
    if value is None:
        raise AdapterConfigurationError(
            "telegram_chat_id_missing", "Telegram did not return a chat id"
        )
    return int(value)


def _is_supergroup(entity: Any) -> bool:
    if entity is None:
        return False
    if bool(getattr(entity, "broadcast", False)) and not bool(
        getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False)
    ):
        return False
    # An explicit megagroup/gigagroup flag is authoritative.
    if hasattr(entity, "megagroup") or hasattr(entity, "gigagroup"):
        return bool(getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False))
    return not bool(getattr(entity, "broadcast", False))


def _participant_is_member(participant: Any) -> bool:
    if participant is None:
        return False
    name = participant.__class__.__name__.lower()
    if any(marker in name for marker in ("notparticipant", "forbidden", "banned", "kicked")):
        return False
    return True


def _participant_is_admin(participant: Any) -> bool:
    if participant is None:
        return False
    name = participant.__class__.__name__.lower()
    if "creator" in name or "admin" in name:
        return True
    rights = getattr(participant, "admin_rights", None)
    return rights is not None


def _participant_is_creator(participant: Any) -> bool:
    """Return whether Telegram identifies the account as the group creator."""

    if participant is None:
        return False
    return "creator" in participant.__class__.__name__.lower()


def _rights_snapshot(rights: Any) -> dict[str, bool]:
    if rights is None:
        return {}
    return {
        name: bool(getattr(rights, name, False))
        for name in _ADMIN_RIGHT_NAMES
        if hasattr(rights, name)
    }


def _admin_rights(permissions: Mapping[str, Any]) -> Any:
    """Build ChatAdminRights while ignoring unknown UI-only keys."""

    from telethon import types

    values = {key: bool(value) for key, value in permissions.items() if key in _ADMIN_RIGHT_NAMES}
    if not any(values.values()):
        raise AdapterConfigurationError(
            "admin_permissions_missing", "At least one admin permission is required"
        )
    return types.ChatAdminRights(**values)


def _requested_admin_contract(
    item: OwnedGroupOperationItem,
) -> tuple[dict[str, bool], str | None]:
    """Validate and normalize the immutable admin assignment snapshot."""

    title = str(item.admin_title or "").strip() or None
    if title and len(title) > 16:
        raise AdapterConfigurationError(
            "admin_title_invalid", "Telegram admin title must be 16 characters or fewer"
        )
    try:
        raw_permissions = json.loads(item.admin_permissions or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AdapterConfigurationError("admin_permissions_invalid") from exc
    if not isinstance(raw_permissions, Mapping):
        raise AdapterConfigurationError("admin_permissions_invalid")
    unknown = set(raw_permissions) - set(_ADMIN_RIGHT_NAMES)
    if unknown:
        raise AdapterConfigurationError("admin_permissions_invalid")
    permissions = {
        key: bool(raw_permissions.get(key, False))
        for key in _ADMIN_RIGHT_NAMES
        if key in raw_permissions
    }
    if not any(permissions.values()):
        raise AdapterConfigurationError(
            "admin_permissions_missing", "At least one admin permission is required"
        )
    return permissions, title


class TelethonOwnedGroupTelegramAdapter:
    """Concrete, gate-checked adapter used by the owned-group worker.

    ``pool`` and all safety readers are injectable.  The default constructor is
    intentionally safe: ``OWNED_GROUP_EXECUTION_ENABLED`` defaults to false,
    and an uninitialized Redis safety state is treated as unavailable.
    """

    # The Celery task also asks the Worker for a DB-level strict precheck before
    # invoking this adapter.  The adapter repeats the gate immediately before
    # each mutating RPC; both layers are intentional.
    requires_strict_precheck = True

    def __init__(
        self,
        db: Any | None = None,
        pool: AccountPoolLike | None = None,
        *,
        bot_client_factory: BotClientFactory | None = None,
        safety_precheck: SafetyPrecheck | None = None,
        safety_state_reader: SafetyStateReader | None = None,
        execution_enabled_reader: ExecutionEnabledReader | None = None,
        execution_service: TelegramExecutionService | None = None,
        require_redis_safety: bool = True,
        now: Callable[[], datetime] | None = None,
    ):
        self.db = db
        self.pool = pool or get_account_pool()
        self.bot_client_factory = bot_client_factory or self._default_bot_client_factory
        self.safety_precheck = safety_precheck or precheck_owned_group_resources
        self.safety_state_reader = safety_state_reader or get_safety_gate_state
        self.execution_enabled_reader = execution_enabled_reader or is_owned_group_execution_enabled
        self.execution_service = execution_service or TelegramExecutionService()
        self.require_redis_safety = require_redis_safety
        self._now = now or datetime.utcnow

    @staticmethod
    def _default_bot_client_factory(token: str) -> Any:
        from app.integrations.telegram.client import TelegramClient, TelegramConfig

        return TelegramClient(TelegramConfig(bot_token=token))

    async def _call(self, value: Any) -> Any:
        return await value if inspect.isawaitable(value) else value

    async def _execution_enabled(self) -> bool:
        return bool(await self._call(self.execution_enabled_reader()))

    @asynccontextmanager
    async def _risk_operation(
        self,
        account: Any,
        action: AccountRiskAction,
        *,
        target_type: str,
        target_id: Any,
        details: Mapping[str, Any] | None = None,
    ):
        """Apply the central account risk budget to every Telegram write."""

        operation = getattr(self.execution_service, "_risk_operation", None)
        if operation is None:
            yield
            return
        async with operation(
            account,
            action,
            target_type=target_type,
            target_id=target_id,
            details=dict(details or {}),
        ):
            yield

    async def _assert_gate(
        self,
        *,
        resources: list[dict[str, Any]] | None = None,
        owner_account_id: int | None = None,
        mutating: bool = True,
    ) -> None:
        """Check every write immediately before it is sent to Telegram.

        Any inability to read the safety state or strict eligibility result is
        treated as a block.  A degraded safety service must never turn into a
        best-effort Telegram write.
        """

        if not await self._execution_enabled():
            raise AdapterBlockedError("owned_group_execution_disabled")
        if not is_safety_gate_enabled():
            raise AdapterBlockedError("safety_gate_disabled")
        try:
            state = await self._call(self.safety_state_reader())
        except Exception as exc:
            raise AdapterBlockedError(
                "safety_gate_backend_unavailable",
                sanitize_telegram_error(exc),
            ) from exc
        if state is None:
            raise AdapterBlockedError("safety_gate_backend_unavailable")
        global_stop = bool(getattr(state, "global_stop", False))
        backend_available = bool(getattr(state, "backend_available", True))
        if isinstance(state, Mapping):
            global_stop = bool(state.get("global_stop", False))
            backend_available = bool(state.get("backend_available", True))
        if global_stop:
            raise AdapterBlockedError("global_stop_enabled")
        if self.require_redis_safety and not backend_available:
            raise AdapterBlockedError("safety_gate_backend_unavailable")
        if not mutating or resources is None or owner_account_id is None:
            return
        try:
            decision = await self.safety_precheck(
                self.db,
                resources,
                int(owner_account_id),
                require_execution=True,
                require_runtime_ready=True,
            )
        except Exception as exc:
            raise AdapterBlockedError(
                "resource_precheck_unavailable",
                sanitize_telegram_error(exc),
            ) from exc
        allowed = bool(getattr(decision, "allowed", False))
        reason = str(getattr(decision, "reason", "resource_eligibility_failed"))
        if isinstance(decision, Mapping):
            allowed = bool(decision.get("allowed", False))
            reason = str(decision.get("reason") or "resource_eligibility_failed")
        if not allowed:
            raise AdapterBlockedError(reason)

    async def _account(self, account_id: int | None) -> TelegramAccount | None:
        if self.db is None or account_id is None or not hasattr(self.db, "get"):
            return None
        return await self.db.get(TelegramAccount, int(account_id))

    async def _acquire(self, account: TelegramAccount | None, *, purpose: str) -> Any:
        if account is None:
            raise AdapterConfigurationError("account_not_found")
        try:
            if hasattr(self.pool, "add_account_from_db"):
                await self.pool.add_account_from_db(account)
            wrapper = await self.pool.acquire_by_id(
                int(account.id),
                purpose=purpose,
                require_session=True,
                raise_on_lease_failure=True,
            )
        except (AccountOperationLeaseBusy, AccountOperationLeaseUnavailable):
            raise
        except Exception as exc:
            raise AdapterConfigurationError(
                "account_acquire_failed", sanitize_telegram_error(exc)
            ) from exc
        if wrapper is None:
            raise AdapterConfigurationError("account_unavailable")
        client = getattr(wrapper, "client", None)
        if client is None and hasattr(wrapper, "get_client"):
            client = wrapper.get_client()
        if client is None:
            await self._release(wrapper)
            raise AdapterConfigurationError("telegram_client_unavailable")
        return wrapper

    async def _release(self, wrapper: Any) -> None:
        if wrapper is None:
            return
        try:
            await self.pool.release(wrapper)
        except Exception:
            # Release failures are logged by AccountPool.  Never replace the
            # operation result with a credential-bearing transport exception.
            return

    @staticmethod
    def _client(wrapper: Any) -> Any:
        client = getattr(wrapper, "client", None)
        if client is None and hasattr(wrapper, "get_client"):
            client = wrapper.get_client()
        return client

    async def _target_resource(
        self, item: OwnedGroupOperationItem
    ) -> tuple[Any, TelegramAccount | None, OwnedBotProfile | None]:
        resource_type = _enum_value(item.resource_type).strip().lower()
        if resource_type == ResourceType.USER.value:
            account = await self._account(item.resource_id)
            if account is None:
                raise AdapterConfigurationError("resource_not_found")
            return account, account, None
        if resource_type != ResourceType.BOT.value:
            raise AdapterConfigurationError("resource_type_invalid")
        if self.db is None or not hasattr(self.db, "get"):
            raise AdapterConfigurationError("bot_profile_not_found")
        profile = await self.db.get(OwnedBotProfile, int(item.resource_id))
        if profile is None:
            raise AdapterConfigurationError("bot_profile_not_found")
        # A Bot API profile is not a Telethon user session.  The linked
        # TelegramAccount is retained for ownership/eligibility checks, but the
        # adapter must never acquire a target AccountPool lease for it.
        account = await self._account(profile.account_id)
        if account is None:
            raise AdapterConfigurationError("bot_account_not_found")
        return profile, None, profile

    async def _bot_user_id(self, profile: OwnedBotProfile) -> int:
        if profile.bot_user_id:
            return int(profile.bot_user_id)
        try:
            token = decrypt_ephemeral_secret(profile.token_ciphertext)
        except Exception as exc:
            raise AdapterConfigurationError("bot_token_invalid") from exc
        if not token:
            raise AdapterConfigurationError("bot_token_missing")
        bot = self.bot_client_factory(token)
        try:
            me = await self._call(bot.get_me())
            user_id = int(getattr(me, "user_id", None) or getattr(me, "id", None) or 0)
            if not user_id:
                raise AdapterConfigurationError("bot_identity_missing")
            profile.bot_user_id = user_id
            username = getattr(me, "username", None)
            if username:
                profile.bot_username = str(username)
            await self._flush()
            return user_id
        finally:
            close = getattr(bot, "close", None)
            if close is not None:
                await self._call(close())

    async def _target_entity(
        self, owner_client: Any, target: Any, target_wrapper: Any | None
    ) -> tuple[Any, int]:
        if isinstance(target, OwnedBotProfile):
            user_id = await self._bot_user_id(target)
            try:
                entity = await self._call(owner_client.get_entity(user_id))
            except Exception as first_exc:
                # Bot API ``getMe`` gives us an id but not a Telethon access
                # hash.  A fresh owner session therefore may not resolve the
                # bare id.  Retry through the verified username, which lets
                # Telethon resolve an InputPeer and preserves the access hash;
                # never silently send a raw integer to an invite/admin RPC.
                username = str(getattr(target, "bot_username", "") or "").strip().lstrip("@")
                entity = None
                if username:
                    for resolver_name in ("get_input_entity", "get_entity"):
                        resolver = getattr(owner_client, resolver_name, None)
                        if resolver is None:
                            continue
                        try:
                            entity = await self._call(resolver(f"@{username}"))
                            break
                        except Exception:
                            continue
                if entity is None:
                    raise AdapterConfigurationError(
                        "bot_peer_unavailable",
                        "Bot peer is not available in the owner Telegram session; open the bot chat or sync its username before retrying",
                    ) from first_exc
            return entity, user_id
        client = self._client(target_wrapper) if target_wrapper is not None else None
        if client is None:
            raise AdapterConfigurationError("telegram_client_unavailable")
        me = await self._call(client.get_me())
        user_id = int(getattr(me, "id", None) or getattr(me, "user_id", None) or 0)
        if not user_id:
            raise AdapterConfigurationError("telegram_user_id_missing")
        return me, user_id

    async def _group_entity(self, client: Any, asset: OwnedGroupAsset) -> Any:
        if not asset.telegram_chat_id:
            raise AdapterConfigurationError("owned_group_chat_missing")
        try:
            return await self._call(client.get_entity(int(asset.telegram_chat_id)))
        except AttributeError:
            return int(asset.telegram_chat_id)

    async def _participant(self, client: Any, entity: Any, user: Any) -> Any | None:
        try:
            from telethon import functions

            result = await self._call(
                client(functions.channels.GetParticipantRequest(channel=entity, participant=user))
            )
            return getattr(result, "participant", result)
        except Exception as exc:
            if _exception_name(exc) in {"usernotparticipanterror", "usernotparticipantserror"}:
                return None
            raise

    async def _flush(self) -> None:
        if self.db is not None and hasattr(self.db, "flush"):
            await self.db.flush()

    async def _commit(self) -> None:
        """Durably fence local state before a remote invite mutation."""

        commit = getattr(self.db, "commit", None) if self.db is not None else None
        if not callable(commit):
            raise InviteLinkMutationError("invite_link_persistence_unavailable")
        try:
            await commit()
        except Exception as exc:  # pragma: no cover - database-specific failure
            raise InviteLinkMutationError("invite_link_persistence_failed") from exc

    async def _persist_membership(
        self,
        asset: OwnedGroupAsset,
        item: OwnedGroupOperationItem,
        *,
        telegram_user_id: int,
        status: str,
        is_admin: bool = False,
        permissions: Mapping[str, Any] | None = None,
        admin_title: str | None = None,
    ) -> None:
        if self.db is None or not hasattr(self.db, "scalar"):
            return
        resource_type = _enum_value(item.resource_type)
        resource_id = int(item.resource_id)
        membership = await self.db.scalar(
            select(OwnedGroupMembership).where(
                OwnedGroupMembership.group_asset_id == int(asset.id),
                OwnedGroupMembership.resource_type == resource_type,
                OwnedGroupMembership.resource_id == resource_id,
            )
        )
        now = self._now()
        snapshot = json.dumps(dict(permissions or {}), sort_keys=True, separators=(",", ":"))
        if membership is None:
            membership = OwnedGroupMembership(
                group_asset_id=asset.id,
                resource_type=resource_type,
                resource_id=resource_id,
                telegram_user_id=int(telegram_user_id),
            )
            self.db.add(membership)
        membership.telegram_user_id = int(telegram_user_id)
        membership.status = status
        membership.is_admin = bool(is_admin)
        membership.admin_title = admin_title
        membership.permissions_snapshot = snapshot
        if status in {
            ItemStatus.MEMBER_VERIFIED.value,
            ItemStatus.ADMIN_VERIFIED.value,
            ItemStatus.SKIPPED_ALREADY_MEMBER.value,
        }:
            membership.joined_at = membership.joined_at or now
            membership.last_verified_at = now
        if item.admin_required:
            assignment = await self.db.scalar(
                select(OwnedGroupAdminAssignment).where(
                    OwnedGroupAdminAssignment.group_asset_id == int(asset.id),
                    OwnedGroupAdminAssignment.resource_type == resource_type,
                    OwnedGroupAdminAssignment.resource_id == resource_id,
                )
            )
            if assignment is None:
                assignment = OwnedGroupAdminAssignment(
                    group_asset_id=asset.id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    permissions_snapshot=snapshot,
                    admin_title=admin_title,
                )
                self.db.add(assignment)
            assignment.permissions_snapshot = snapshot
            assignment.admin_title = admin_title
            assignment.status = "verified" if is_admin else "pending"
            assignment.verified_at = now if is_admin else None
        await self._flush()

    async def _persist_invite(
        self, asset: OwnedGroupAsset, link: str, *, request_needed: bool
    ) -> None:
        if self.db is None or not hasattr(self.db, "scalar"):
            return
        encrypted = encrypt_ephemeral_secret(link)
        if not encrypted:
            raise AdapterConfigurationError("invite_link_encryption_failed")
        existing = await self.db.scalar(
            select(OwnedGroupInviteLink).where(
                OwnedGroupInviteLink.group_asset_id == int(asset.id),
                OwnedGroupInviteLink.is_active.is_(True),
            )
        )
        if existing is None:
            self.db.add(
                OwnedGroupInviteLink(
                    group_asset_id=asset.id,
                    link_type="join_request" if request_needed else "member_invite",
                    link_ciphertext=encrypted,
                    is_active=True,
                )
            )
        else:
            existing.link_ciphertext = encrypted
            existing.link_type = "join_request" if request_needed else "member_invite"
            existing.is_active = True
            existing.revoked_at = None
        await self._flush()

    async def _active_invite_link(self, asset: OwnedGroupAsset) -> str:
        if self.db is None or not hasattr(self.db, "scalar"):
            raise AdapterConfigurationError("invite_link_missing")
        link = await self.db.scalar(
            select(OwnedGroupInviteLink).where(
                OwnedGroupInviteLink.group_asset_id == int(asset.id),
                OwnedGroupInviteLink.is_active.is_(True),
            )
        )
        if link is None:
            raise AdapterConfigurationError("invite_link_missing")
        try:
            value = decrypt_ephemeral_secret(link.link_ciphertext)
        except Exception as exc:
            raise AdapterConfigurationError("invite_link_invalid") from exc
        if not value:
            raise AdapterConfigurationError("invite_link_invalid")
        return value

    async def _reconcile_existing_group(
        self,
        *,
        asset: OwnedGroupAsset,
        owner: TelegramAccount,
        wrapper: Any,
        client: Any,
        entity: Any,
    ) -> tuple[str | None, str | None]:
        """Validate and finish setup for a group created before a timeout.

        A persisted ``telegram_chat_id`` is only an idempotency hint.  It is not
        sufficient evidence that the current owner still controls the group or
        that the requested public/private access contract was completed.  This
        helper performs read verification and only repairs a missing public
        username or private invite after the normal immediate safety gate.
        """

        if not _is_supergroup(entity):
            raise AdapterConfigurationError("existing_chat_not_supergroup")

        get_me = getattr(client, "get_me", None)
        if not callable(get_me):
            raise AdapterConfigurationError("owner_identity_unavailable")
        owner_me = await self._call(get_me())
        owner_user_id = int(
            getattr(owner_me, "id", None) or getattr(owner_me, "user_id", None) or 0
        )
        if not owner_user_id:
            raise AdapterConfigurationError("owner_identity_unavailable")
        owner_participant = await self._participant(client, entity, owner_me)
        if not _participant_is_member(owner_participant):
            raise AdapterConfigurationError("owner_not_group_member")
        if not _participant_is_admin(owner_participant):
            raise AdapterConfigurationError("owner_not_group_admin")
        if not _participant_is_creator(owner_participant):
            raise AdapterConfigurationError("owner_not_group_creator")

        remote_username = str(getattr(entity, "username", None) or "").strip().lstrip("@")
        desired_username = str(asset.telegram_username or "").strip().lstrip("@")
        if asset.visibility == "public":
            if not desired_username or not _PUBLIC_USERNAME_RE.fullmatch(desired_username):
                raise AdapterConfigurationError("public_username_invalid")
            if remote_username and remote_username.lower() != desired_username.lower():
                # A different remote username may belong to an operator's manual
                # change.  Never overwrite it as part of an uncertain retry.
                raise AdapterConfigurationError("existing_username_mismatch")
            if not remote_username:
                from telethon import functions

                await self._assert_gate(
                    resources=[
                        {
                            "resource_type": ResourceType.USER.value,
                            "resource_id": int(asset.owner_account_id),
                        }
                    ],
                    owner_account_id=int(asset.owner_account_id),
                )
                async with self._risk_operation(
                    wrapper,
                    AccountRiskAction.PROFILE_UPDATE,
                    target_type="group",
                    target_id=_telegram_peer_id(entity),
                    details={"source": "owned_group_existing_public_username"},
                ):
                    try:
                        await self._call(
                            client(
                                functions.channels.UpdateUsernameRequest(
                                    channel=entity, username=desired_username
                                )
                            )
                        )
                    except Exception as exc:
                        classification = classify_telegram_error(exc, mutating=True, phase="create")
                        raise AdapterConfigurationError(
                            classification.reason_code, classification.message
                        ) from exc
                remote_username = desired_username
            asset.telegram_username = remote_username
            asset.public_link = f"https://t.me/{remote_username}"
            await self._flush()
            return remote_username, asset.public_link

        # Private groups must always have a recoverable primary invite, even
        # when the member strategy is direct_invite.  Reuse a valid local row;
        # export only when no valid active row exists.
        request_needed = asset.invite_mode == "manual_approval"
        active = None
        if self.db is not None and hasattr(self.db, "scalar"):
            active = await self.db.scalar(
                select(OwnedGroupInviteLink).where(
                    OwnedGroupInviteLink.group_asset_id == int(asset.id),
                    OwnedGroupInviteLink.is_active.is_(True),
                )
            )
        if active is not None:
            try:
                await self._invite_plaintext(active)
                return None, None
            except InviteLinkMutationError:
                # The ciphertext is corrupt/empty; replace the row only after a
                # fresh remote export is confirmed.
                pass
        link = await self._export_remote_invite(
            asset=asset,
            wrapper=wrapper,
            client=client,
            entity=entity,
            request_needed=request_needed,
        )
        await self._persist_invite(asset, link, request_needed=request_needed)
        return None, None

    async def _invite_plaintext(self, invite: OwnedGroupInviteLink) -> str:
        try:
            value = decrypt_ephemeral_secret(invite.link_ciphertext)
        except Exception as exc:
            raise InviteLinkMutationError("invite_link_invalid") from exc
        value = str(value or "").strip()
        if not value or not _PRIVATE_INVITE_RE.fullmatch(value):
            raise InviteLinkMutationError("invite_link_invalid")
        return value

    async def _invite_context(
        self, asset: OwnedGroupAsset, *, purpose: str
    ) -> tuple[TelegramAccount, Any, Any, Any]:
        """Acquire the owner session and resolve the remote group for one write."""

        if not asset.telegram_chat_id:
            raise InviteLinkMutationError("owned_group_chat_missing")
        owner = await self._account(asset.owner_account_id)
        if owner is None:
            raise InviteLinkMutationError("owner_account_not_found")
        await self._assert_gate(
            resources=[
                {
                    "resource_type": ResourceType.USER.value,
                    "resource_id": int(asset.owner_account_id),
                }
            ],
            owner_account_id=int(asset.owner_account_id),
        )
        wrapper = await self._acquire(owner, purpose=purpose)
        try:
            client = self._client(wrapper)
            if client is None:
                raise InviteLinkMutationError("telegram_client_unavailable")
            entity = await self._group_entity(client, asset)
            get_me = getattr(client, "get_me", None)
            if not callable(get_me):
                raise AdapterConfigurationError("owner_identity_unavailable")
            owner_me = await self._call(get_me())
            owner_participant = await self._participant(client, entity, owner_me)
            if not _participant_is_member(owner_participant):
                raise AdapterConfigurationError("owner_not_group_member")
            if not _participant_is_admin(owner_participant):
                raise AdapterConfigurationError("owner_not_group_admin")
            if not _participant_is_creator(owner_participant):
                raise AdapterConfigurationError("owner_not_group_creator")
            return owner, wrapper, client, entity
        except Exception:
            # The tuple is not returned on failure, so the caller cannot see the
            # wrapper and clean it up in its own finally block.
            await self._release(wrapper)
            raise

    async def _revoke_remote_invite(
        self,
        *,
        asset: OwnedGroupAsset,
        wrapper: Any,
        client: Any,
        entity: Any,
        link: str,
    ) -> None:
        from telethon import functions

        await self._assert_gate(
            resources=[
                {
                    "resource_type": ResourceType.USER.value,
                    "resource_id": int(asset.owner_account_id),
                }
            ],
            owner_account_id=int(asset.owner_account_id),
        )
        async with self._risk_operation(
            wrapper,
            AccountRiskAction.JOIN,
            target_type="group",
            target_id=_telegram_peer_id(entity),
            details={"source": "owned_group_invite_revoke"},
        ):
            await self._call(
                client(
                    functions.messages.EditExportedChatInviteRequest(
                        peer=entity,
                        link=link,
                        revoked=True,
                    )
                )
            )

    async def _export_remote_invite(
        self,
        *,
        asset: OwnedGroupAsset,
        wrapper: Any,
        client: Any,
        entity: Any,
        request_needed: bool,
    ) -> str:
        from telethon import functions

        await self._assert_gate(
            resources=[
                {
                    "resource_type": ResourceType.USER.value,
                    "resource_id": int(asset.owner_account_id),
                }
            ],
            owner_account_id=int(asset.owner_account_id),
        )
        async with self._risk_operation(
            wrapper,
            AccountRiskAction.JOIN,
            target_type="group",
            target_id=_telegram_peer_id(entity),
            details={"source": "owned_group_invite_regenerate"},
        ):
            result = await self._call(
                client(
                    functions.messages.ExportChatInviteRequest(
                        peer=entity,
                        request_needed=request_needed,
                    )
                )
            )
        link = str(getattr(result, "link", "") or "").strip()
        if not link or not _PRIVATE_INVITE_RE.fullmatch(link):
            raise InviteLinkMutationError("invite_link_invalid")
        return link

    async def revoke_invite_link(
        self,
        asset: OwnedGroupAsset,
        invite: OwnedGroupInviteLink,
    ) -> dict[str, Any]:
        """Revoke one private invite remotely, then mark its DB row inactive."""

        if asset.visibility != "private":
            raise InviteLinkMutationError("public_link_not_revocable")
        if not invite.is_active:
            raise InviteLinkMutationError("invite_link_not_active")
        link = await self._invite_plaintext(invite)
        wrapper = None
        try:
            _owner, wrapper, client, entity = await self._invite_context(
                asset, purpose="owned_group_invite_revoke"
            )
            # Fence the local bearer before the remote write.  If the process or
            # database fails after Telegram accepts the revoke, the old URL is
            # still durably hidden instead of being advertised as active.
            invite.is_active = False
            invite.revoked_at = self._now()
            await self._flush()
            await self._commit()
            try:
                await self._revoke_remote_invite(
                    asset=asset, wrapper=wrapper, client=client, entity=entity, link=link
                )
            except (AdapterBlockedError, AdapterConfigurationError):
                raise
            except InviteLinkMutationError:
                raise
            except Exception as exc:
                classification = classify_telegram_error(exc, mutating=True, phase="invite")
                raise InviteLinkMutationError(
                    classification.reason_code, classification.message
                ) from exc
        finally:
            await self._release(wrapper)
        return {
            "success": True,
            "invite_id": int(invite.id),
            "status": "revoked",
        }

    async def regenerate_invite_link(
        self,
        asset: OwnedGroupAsset,
        *,
        invite: OwnedGroupInviteLink | None = None,
        request_needed: bool | None = None,
        created_by: int | None = None,
    ) -> dict[str, Any]:
        """Rotate the current private invite with remote-first fencing.

        The old link is marked inactive immediately after a confirmed remote
        revoke and before exporting the replacement.  Thus an export failure
        cannot leave the database advertising a bearer link that Telegram has
        already invalidated.
        """

        if asset.visibility != "private":
            raise InviteLinkMutationError("public_link_not_regenerable")
        if not asset.telegram_chat_id:
            raise InviteLinkMutationError("owned_group_chat_missing")
        active = invite
        if active is None and self.db is not None and hasattr(self.db, "scalar"):
            active = await self.db.scalar(
                select(OwnedGroupInviteLink).where(
                    OwnedGroupInviteLink.group_asset_id == int(asset.id),
                    OwnedGroupInviteLink.is_active.is_(True),
                )
            )
        old_link = None
        if active is not None:
            try:
                old_link = await self._invite_plaintext(active)
            except InviteLinkMutationError:
                # A corrupt local ciphertext cannot be used for a remote revoke;
                # hide it and export a fresh link rather than blocking recovery.
                old_link = None
        # Fail before any Telegram RPC if the encryption key is unavailable.
        if not encrypt_ephemeral_secret("owned-group-invite-key-check"):
            raise InviteLinkMutationError("invite_link_encryption_failed")

        wrapper = None
        new_link: str | None = None
        try:
            _owner, wrapper, client, entity = await self._invite_context(
                asset, purpose="owned_group_invite_regenerate"
            )
            if active is not None:
                # Commit the local fence before either remote revoke or export.
                # A failed export must leave no stale bearer link visible.
                active.is_active = False
                active.revoked_at = self._now()
                await self._flush()
                await self._commit()
            if active is not None and old_link is not None:
                try:
                    await self._revoke_remote_invite(
                        asset=asset,
                        wrapper=wrapper,
                        client=client,
                        entity=entity,
                        link=old_link,
                    )
                except (AdapterBlockedError, AdapterConfigurationError):
                    raise
                except InviteLinkMutationError:
                    raise
                except Exception as exc:
                    classification = classify_telegram_error(exc, mutating=True, phase="invite")
                    raise InviteLinkMutationError(
                        classification.reason_code, classification.message
                    ) from exc
            needed = (
                bool(request_needed)
                if request_needed is not None
                else str(asset.invite_mode or "").strip().lower() == "manual_approval"
            )
            try:
                new_link = await self._export_remote_invite(
                    asset=asset,
                    wrapper=wrapper,
                    client=client,
                    entity=entity,
                    request_needed=needed,
                )
            except (AdapterBlockedError, AdapterConfigurationError):
                raise
            except InviteLinkMutationError:
                raise
            except Exception as exc:
                classification = classify_telegram_error(exc, mutating=True, phase="invite")
                raise InviteLinkMutationError(
                    classification.reason_code, classification.message
                ) from exc
        finally:
            await self._release(wrapper)

        encrypted = encrypt_ephemeral_secret(new_link or "")
        if not encrypted:
            raise InviteLinkMutationError("invite_link_encryption_failed")
        replacement = OwnedGroupInviteLink(
            group_asset_id=int(asset.id),
            link_type="join_request"
            if (
                request_needed
                if request_needed is not None
                else str(asset.invite_mode or "").strip().lower() == "manual_approval"
            )
            else "member_invite",
            link_ciphertext=encrypted,
            is_active=True,
            created_by=created_by,
        )
        if self.db is None or not hasattr(self.db, "add"):
            raise InviteLinkMutationError("invite_link_persistence_unavailable")
        self.db.add(replacement)
        try:
            await self._flush()
        except Exception as exc:
            raise InviteLinkMutationError("invite_link_persistence_failed") from exc
        return {
            "success": True,
            "invite_id": int(replacement.id) if replacement.id else None,
            "status": "active",
            "link_type": replacement.link_type,
        }

    async def preflight(
        self, asset: OwnedGroupAsset, owner: TelegramAccount | None
    ) -> PreflightResult:
        """Perform strict checks without creating a Telegram object."""

        resources = [
            {"resource_type": ResourceType.USER.value, "resource_id": int(asset.owner_account_id)}
        ]
        try:
            await self._assert_gate(
                resources=resources,
                owner_account_id=int(asset.owner_account_id),
                mutating=True,
            )
            if owner is None:
                raise AdapterConfigurationError("owner_account_not_found")
            if _enum_value(owner.account_type) != AccountType.PROMOTER.value:
                raise AdapterConfigurationError("owner_account_type_invalid")
            if not owner.is_active:
                raise AdapterConfigurationError("owner_account_inactive")
            if asset.visibility == "public" and not str(asset.telegram_username or "").strip():
                raise AdapterConfigurationError("public_username_required")
            if asset.invite_mode not in {"direct_invite", "link_self_join", "manual_approval"}:
                raise AdapterConfigurationError("invite_mode_invalid")
            return PreflightResult(ready=True, reason_code="eligible")
        except AdapterBlockedError as exc:
            return PreflightResult(ready=False, reason_code=exc.reason_code, message=exc.message)
        except AdapterConfigurationError as exc:
            return PreflightResult(ready=False, reason_code=exc.reason_code, message=exc.message)
        except Exception as exc:
            return PreflightResult(
                ready=False,
                reason_code="preflight_exception",
                message=sanitize_telegram_error(exc),
            )

    async def create_group(
        self, asset: OwnedGroupAsset, owner: TelegramAccount | None
    ) -> GroupCreateResult:
        """Create or reconcile a Telegram *supergroup*, never a broadcast channel."""

        wrapper = None
        chat_id: int | None = int(asset.telegram_chat_id) if asset.telegram_chat_id else None
        try:
            await self._assert_gate(
                resources=[
                    {
                        "resource_type": ResourceType.USER.value,
                        "resource_id": int(asset.owner_account_id),
                    }
                ],
                owner_account_id=int(asset.owner_account_id),
            )
            if owner is None:
                raise AdapterConfigurationError("owner_account_not_found")
            wrapper = await self._acquire(owner, purpose="owned_group_create")
            client = self._client(wrapper)
            if client is None:
                raise AdapterConfigurationError("telegram_client_unavailable")
            if chat_id:
                entity = await self._group_entity(client, asset)
                username, public_link = await self._reconcile_existing_group(
                    asset=asset,
                    owner=owner,
                    wrapper=wrapper,
                    client=client,
                    entity=entity,
                )
                return GroupCreateResult(
                    success=True,
                    telegram_chat_id=chat_id,
                    telegram_username=username,
                    public_link=public_link,
                    reason_code="already_created",
                )

            from telethon import functions

            await self._assert_gate(
                resources=[
                    {
                        "resource_type": ResourceType.USER.value,
                        "resource_id": int(asset.owner_account_id),
                    }
                ],
                owner_account_id=int(asset.owner_account_id),
            )
            async with self._risk_operation(
                wrapper,
                AccountRiskAction.CHANNEL_CREATE,
                target_type="account",
                target_id=int(owner.id),
                details={
                    "source": "owned_group_create",
                    "title_length": len(str(asset.title or "")),
                },
            ):
                result = await self._call(
                    client(
                        functions.channels.CreateChannelRequest(
                            title=str(asset.title).strip(),
                            about=str(asset.about or "").strip(),
                            broadcast=False,
                            megagroup=True,
                        )
                    )
                )
            chats = list(getattr(result, "chats", None) or [])
            if not chats:
                raise AdapterConfigurationError("telegram_group_not_returned")
            entity = chats[0]
            chat_id = _telegram_peer_id(entity)
            # Persist the remote id before optional username/invite setup.  If a
            # later RPC fails, reconciliation can inspect this group instead of
            # creating a second one.
            asset.telegram_chat_id = chat_id
            await self._flush()
            if not _is_supergroup(entity):
                raise AdapterConfigurationError("created_chat_not_supergroup")

            username = str(asset.telegram_username or "").strip().lstrip("@")
            if asset.visibility == "public":
                if not username or not _PUBLIC_USERNAME_RE.fullmatch(username):
                    raise AdapterConfigurationError("public_username_invalid")
                await self._assert_gate(
                    resources=[
                        {
                            "resource_type": ResourceType.USER.value,
                            "resource_id": int(asset.owner_account_id),
                        }
                    ],
                    owner_account_id=int(asset.owner_account_id),
                )
                async with self._risk_operation(
                    wrapper,
                    AccountRiskAction.PROFILE_UPDATE,
                    target_type="group",
                    target_id=chat_id,
                    details={"source": "owned_group_public_username"},
                ):
                    await self._call(
                        client(
                            functions.channels.UpdateUsernameRequest(
                                channel=entity, username=username
                            )
                        )
                    )
                asset.telegram_username = username
                asset.public_link = f"https://t.me/{username}"
            else:
                # Every private asset gets one encrypted primary invite, even
                # when its member strategy is direct_invite.  The link is the
                # operator-visible recovery/backup path required by the UI;
                # direct invitation can still be used for the actual batch.
                await self._assert_gate(
                    resources=[
                        {
                            "resource_type": ResourceType.USER.value,
                            "resource_id": int(asset.owner_account_id),
                        }
                    ],
                    owner_account_id=int(asset.owner_account_id),
                )
                request_needed = asset.invite_mode == "manual_approval"
                async with self._risk_operation(
                    wrapper,
                    AccountRiskAction.JOIN,
                    target_type="group",
                    target_id=chat_id,
                    details={"source": "owned_group_invite_link"},
                ):
                    invite = await self._call(
                        client(
                            functions.messages.ExportChatInviteRequest(
                                peer=entity,
                                request_needed=request_needed,
                            )
                        )
                    )
                link = getattr(invite, "link", None)
                if not link:
                    raise AdapterConfigurationError("invite_link_missing")
                await self._persist_invite(asset, str(link), request_needed=request_needed)
            return GroupCreateResult(
                success=True,
                telegram_chat_id=chat_id,
                telegram_username=username or None,
                public_link=asset.public_link if asset.visibility == "public" else None,
                reason_code="created",
            )
        except AdapterBlockedError as exc:
            return GroupCreateResult(
                success=False,
                telegram_chat_id=chat_id,
                reason_code=exc.reason_code,
                message=exc.message,
            )
        except AdapterConfigurationError as exc:
            return GroupCreateResult(
                success=False,
                telegram_chat_id=chat_id,
                reason_code=exc.reason_code,
                message=exc.message,
            )
        except Exception as exc:
            classification = classify_telegram_error(exc, mutating=True, phase="create")
            return GroupCreateResult(
                success=False,
                telegram_chat_id=chat_id,
                reason_code=classification.reason_code,
                message=classification.message,
            )
        finally:
            await self._release(wrapper)

    async def _promote_and_verify(
        self,
        *,
        asset: OwnedGroupAsset,
        item: OwnedGroupOperationItem,
        client: Any,
        entity: Any,
        target_entity: Any,
        target_user_id: int,
        risk_account: Any,
    ) -> tuple[bool, bool, dict[str, bool], str | None]:
        permissions, title = _requested_admin_contract(item)
        rights = _admin_rights(permissions)
        await self._assert_gate(
            resources=[
                {
                    "resource_type": ResourceType.USER.value,
                    "resource_id": int(asset.owner_account_id),
                },
                {
                    "resource_type": _enum_value(item.resource_type),
                    "resource_id": int(item.resource_id),
                },
            ],
            owner_account_id=int(asset.owner_account_id),
        )
        from telethon import functions

        async with self._risk_operation(
            risk_account,
            AccountRiskAction.MODERATION,
            target_type="group",
            target_id=_telegram_peer_id(entity),
            details={"source": "owned_group_admin_promotion", "target_user_id": target_user_id},
        ):
            await self._call(
                client(
                    functions.channels.EditAdminRequest(
                        channel=entity,
                        user_id=target_entity,
                        admin_rights=rights,
                        rank=title,
                    )
                )
            )
        participant = await self._participant(client, entity, target_entity)
        if not _participant_is_member(participant) or not _participant_is_admin(participant):
            return False, False, {}, title
        actual = _rights_snapshot(getattr(participant, "admin_rights", None))
        required = {key: bool(value) for key, value in permissions.items() if bool(value)}
        if required and any(not actual.get(key, False) for key in required):
            return False, True, actual, getattr(participant, "rank", None)
        actual_title = str(getattr(participant, "rank", None) or "").strip() or None
        if title is not None and actual_title != title:
            return False, True, actual, actual_title
        return True, True, actual, actual_title

    async def _execute_direct_invite(
        self,
        *,
        asset: OwnedGroupAsset,
        item: OwnedGroupOperationItem,
        owner_client: Any,
        entity: Any,
        target_entity: Any,
        risk_account: Any,
    ) -> ItemExecutionResult:
        from telethon import functions

        await self._assert_gate(
            resources=[
                {
                    "resource_type": ResourceType.USER.value,
                    "resource_id": int(asset.owner_account_id),
                },
                {
                    "resource_type": _enum_value(item.resource_type),
                    "resource_id": int(item.resource_id),
                },
            ],
            owner_account_id=int(asset.owner_account_id),
        )
        already_member = False
        try:
            async with self._risk_operation(
                risk_account,
                AccountRiskAction.JOIN,
                target_type="group",
                target_id=_telegram_peer_id(entity),
                details={"source": "owned_group_direct_invite"},
            ):
                await self._call(
                    owner_client(
                        functions.channels.InviteToChannelRequest(
                            channel=entity,
                            users=[target_entity],
                        )
                    )
                )
        except Exception as exc:
            classification = classify_telegram_error(exc, mutating=True, phase="invite")
            if classification.reason_code != ReasonCode.ALREADY_MEMBER.value:
                return ItemExecutionResult(
                    status=classification.status,
                    transient=classification.transient,
                    reason_code=classification.reason_code,
                    error_message=classification.message,
                    retry_after_seconds=classification.retry_after_seconds,
                )
            already_member = True

        participant = await self._participant(owner_client, entity, target_entity)
        if not _participant_is_member(participant):
            return ItemExecutionResult(
                status=ItemStatus.INVITE_SENT.value,
                reason_code="invite_sent",
                error_message="Invite sent; membership verification is pending",
            )
        return ItemExecutionResult(
            status=(
                ItemStatus.SKIPPED_ALREADY_MEMBER.value
                if already_member
                else ItemStatus.MEMBER_VERIFIED.value
            ),
            success=True,
            reason_code=ReasonCode.ALREADY_MEMBER.value if already_member else "member_verified",
        )

    async def execute_item(
        self,
        asset: OwnedGroupAsset,
        operation: OwnedGroupOperation,
        item: OwnedGroupOperationItem,
    ) -> ItemExecutionResult:
        """Invite/join one resource, verify membership, then optionally promote it."""

        owner_wrapper = None
        target_wrapper = None
        try:
            owner = await self._account(asset.owner_account_id)
            resources = [
                {
                    "resource_type": ResourceType.USER.value,
                    "resource_id": int(asset.owner_account_id),
                },
                {
                    "resource_type": _enum_value(item.resource_type),
                    "resource_id": int(item.resource_id),
                },
            ]
            await self._assert_gate(
                resources=resources,
                owner_account_id=int(asset.owner_account_id),
            )
            target, target_account, target_profile = await self._target_resource(item)
            owner_wrapper = await self._acquire(owner, purpose="owned_group_owner")
            owner_client = self._client(owner_wrapper)
            entity = await self._group_entity(owner_client, asset)
            if target_profile is None:
                target_wrapper = await self._acquire(target_account, purpose="owned_group_target")
            # Bot profiles are verified through Bot API getMe and are invited by
            # the owner Telethon session; they must not consume a linked
            # GuardianBot AccountPool/Telethon lease.
            target_entity, target_user_id = await self._target_entity(
                owner_client, target, target_wrapper
            )

            participant = await self._participant(owner_client, entity, target_entity)
            if not _participant_is_member(participant):
                mode = str(asset.invite_mode or "direct_invite").strip().lower()
                if target_profile is not None and mode != "direct_invite":
                    raise AdapterConfigurationError("bot_self_join_unsupported")
                if mode == "direct_invite":
                    invited = await self._execute_direct_invite(
                        asset=asset,
                        item=item,
                        owner_client=owner_client,
                        entity=entity,
                        target_entity=target_entity,
                        risk_account=owner_wrapper,
                    )
                    if not invited.success and invited.status != ItemStatus.MEMBER_VERIFIED.value:
                        if invited.status in {
                            ItemStatus.INVITE_SENT.value,
                            ItemStatus.WAITING_APPROVAL.value,
                        }:
                            return invited
                        return invited
                    participant = await self._participant(owner_client, entity, target_entity)
                else:
                    link = await self._active_invite_link(asset)
                    await self._assert_gate(
                        resources=resources, owner_account_id=int(asset.owner_account_id)
                    )
                    try:
                        await self.execution_service.join_group_by_link(
                            target_wrapper,
                            link,
                            source="owned_group_self_join",
                        )
                    except TelegramJoinRequestPendingError as exc:
                        return ItemExecutionResult(
                            status=ItemStatus.WAITING_APPROVAL.value,
                            reason_code="join_request_pending",
                            error_message=sanitize_telegram_error(exc),
                            telegram_user_id=target_user_id,
                        )
                    participant = await self._participant(owner_client, entity, target_entity)
            if not _participant_is_member(participant):
                return ItemExecutionResult(
                    status=ItemStatus.INVITE_SENT.value,
                    reason_code="invite_sent",
                    error_message="Telegram accepted the invite but membership is not yet visible",
                    telegram_user_id=target_user_id,
                )

            if item.admin_required:
                (
                    admin_verified,
                    actual_is_admin,
                    permissions,
                    title,
                ) = await self._promote_and_verify(
                    asset=asset,
                    item=item,
                    client=owner_client,
                    entity=entity,
                    target_entity=target_entity,
                    target_user_id=target_user_id,
                    risk_account=owner_wrapper,
                )
                if not admin_verified:
                    return ItemExecutionResult(
                        status=ItemStatus.ADMIN_PROMOTING.value,
                        reason_code="admin_verification_pending",
                        error_message="Membership is present but administrator rights are not verified",
                        telegram_user_id=target_user_id,
                        membership_verified=True,
                        is_admin=actual_is_admin,
                        admin_permissions=permissions,
                        admin_title=title,
                    )
                status = ItemStatus.ADMIN_VERIFIED.value
                await self._persist_membership(
                    asset,
                    item,
                    telegram_user_id=target_user_id,
                    status=status,
                    is_admin=True,
                    permissions=permissions,
                    admin_title=title,
                )
                return ItemExecutionResult(
                    status=status,
                    success=True,
                    reason_code="admin_verified",
                    telegram_user_id=target_user_id,
                    membership_verified=True,
                    is_admin=True,
                    admin_permissions=permissions,
                    admin_title=title,
                )

            status = ItemStatus.MEMBER_VERIFIED.value
            await self._persist_membership(
                asset,
                item,
                telegram_user_id=target_user_id,
                status=status,
                is_admin=_participant_is_admin(participant),
            )
            return ItemExecutionResult(
                status=status,
                success=True,
                reason_code="member_verified",
                telegram_user_id=target_user_id,
                membership_verified=True,
                is_admin=_participant_is_admin(participant),
                admin_permissions=_rights_snapshot(getattr(participant, "admin_rights", None)),
                admin_title=getattr(participant, "rank", None),
            )
        except (AdapterBlockedError, AdapterConfigurationError) as exc:
            retryable = exc.reason_code in _RETRYABLE_GATE_REASONS
            return ItemExecutionResult(
                status=(
                    ItemStatus.FAILED_TRANSIENT.value
                    if retryable
                    else ItemStatus.FAILED_PERMANENT.value
                ),
                transient=retryable,
                reason_code=exc.reason_code,
                error_message=exc.message,
                retry_after_seconds=60 if retryable else None,
            )
        except Exception as exc:
            classification = classify_telegram_error(exc, mutating=True, phase="item")
            return ItemExecutionResult(
                status=classification.status,
                transient=classification.transient,
                reason_code=classification.reason_code,
                error_message=classification.message,
                retry_after_seconds=classification.retry_after_seconds,
            )
        finally:
            await self._release(target_wrapper)
            await self._release(owner_wrapper)

    async def reconcile_item(
        self,
        asset: OwnedGroupAsset,
        operation: OwnedGroupOperation,
        item: OwnedGroupOperationItem,
    ) -> ItemExecutionResult | None:
        """Read-only membership/admin reconciliation; never re-invites."""

        owner_wrapper = None
        target_wrapper = None
        try:
            owner = await self._account(asset.owner_account_id)
            target, target_account, _profile = await self._target_resource(item)
            owner_wrapper = await self._acquire(owner, purpose="owned_group_reconcile_owner")
            owner_client = self._client(owner_wrapper)
            entity = await self._group_entity(owner_client, asset)
            if _profile is None:
                target_wrapper = await self._acquire(
                    target_account, purpose="owned_group_reconcile_target"
                )
            # Bot reconciliation is owner-side read-only verification plus
            # profile.getMe; no linked bot AccountPool lease is acquired.
            target_entity, target_user_id = await self._target_entity(
                owner_client, target, target_wrapper
            )
            participant = await self._participant(owner_client, entity, target_entity)
            if not _participant_is_member(participant):
                return ItemExecutionResult(
                    status=ItemStatus.UNKNOWN.value,
                    reason_code=ReasonCode.UNKNOWN_NEEDS_RECONCILE.value,
                    error_message="Telegram membership is not yet visible; no retry was attempted",
                    telegram_user_id=target_user_id,
                )
            is_admin = _participant_is_admin(participant)
            rights = _rights_snapshot(getattr(participant, "admin_rights", None))
            title = str(getattr(participant, "rank", None) or "").strip() or None
            if item.admin_required:
                required_permissions, required_title = _requested_admin_contract(item)
                reason_code = None
                error_message = None
                if not is_admin:
                    reason_code = "admin_not_verified"
                    error_message = "Membership is verified but administrator status is missing"
                elif any(
                    enabled and not rights.get(key, False)
                    for key, enabled in required_permissions.items()
                ):
                    reason_code = "admin_permissions_not_verified"
                    error_message = (
                        "Membership is verified but required administrator rights are missing"
                    )
                elif required_title is not None and title != required_title:
                    reason_code = "admin_title_not_verified"
                    error_message = (
                        "Membership is verified but the administrator title does not match"
                    )
                if reason_code is not None:
                    await self._persist_membership(
                        asset,
                        item,
                        telegram_user_id=target_user_id,
                        status=ItemStatus.MEMBER_VERIFIED.value,
                        is_admin=is_admin,
                        permissions=rights,
                        admin_title=title,
                    )
                    return ItemExecutionResult(
                        status=ItemStatus.FAILED_TRANSIENT.value,
                        transient=True,
                        reason_code=reason_code,
                        error_message=error_message,
                        telegram_user_id=target_user_id,
                        membership_verified=True,
                        is_admin=is_admin,
                        admin_permissions=rights,
                        admin_title=title,
                    )
            status = (
                ItemStatus.ADMIN_VERIFIED.value
                if item.admin_required
                else ItemStatus.MEMBER_VERIFIED.value
            )
            await self._persist_membership(
                asset,
                item,
                telegram_user_id=target_user_id,
                status=status,
                is_admin=is_admin,
                permissions=rights,
                admin_title=title,
            )
            return ItemExecutionResult(
                status=status,
                success=True,
                reason_code="admin_verified" if item.admin_required else "member_verified",
                telegram_user_id=target_user_id,
                membership_verified=True,
                is_admin=is_admin,
                admin_permissions=rights,
                admin_title=title,
            )
        except (AdapterBlockedError, AdapterConfigurationError) as exc:
            retryable = exc.reason_code in _RETRYABLE_GATE_REASONS
            return ItemExecutionResult(
                status=(
                    ItemStatus.FAILED_TRANSIENT.value
                    if retryable
                    else ItemStatus.FAILED_PERMANENT.value
                ),
                transient=retryable,
                reason_code=exc.reason_code,
                error_message=exc.message,
                retry_after_seconds=60 if retryable else None,
            )
        except Exception as exc:
            classification = classify_telegram_error(exc, mutating=False, phase="reconcile")
            return ItemExecutionResult(
                status=classification.status,
                transient=classification.transient,
                reason_code=classification.reason_code,
                error_message=classification.message,
                retry_after_seconds=classification.retry_after_seconds,
            )
        finally:
            await self._release(target_wrapper)
            await self._release(owner_wrapper)


# Aliases make the adapter discoverable under the names used in the design docs.
TelethonOwnedGroupAdapter = TelethonOwnedGroupTelegramAdapter
OwnedGroupTelethonAdapter = TelethonOwnedGroupTelegramAdapter


__all__ = [
    "AdapterBlockedError",
    "AdapterConfigurationError",
    "InviteLinkMutationError",
    "OwnedGroupTelethonAdapter",
    "TelegramErrorClassification",
    "TelethonOwnedGroupAdapter",
    "TelethonOwnedGroupTelegramAdapter",
    "classify_telegram_error",
    "classify_telegram_exception",
    "sanitize_telegram_error",
]
