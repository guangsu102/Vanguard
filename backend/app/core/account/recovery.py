"""One-attempt, identity-verified recovery of an ERROR account."""
from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import re
import secrets
import socket
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.account.manager import AccountManager
from app.core.account.models import (
    AccountEnvironmentEvent,
    AccountOperationConfig,
    AccountStatus,
    Proxy,
    TelegramAccount,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.account.pool import AccountPool


@dataclass(frozen=True)
class RecoveryResult:
    ok: bool
    reason: str
    retry_after_seconds: int | None = None


class RecoveryCheckFailed(RuntimeError):
    pass


RECOVERY_RETRYABLE_REASONS = frozenset({
    "connection_timeout", "network_unavailable", "proxy_unavailable", "session_conflict",
    "operation_lease_lost",
})
RECOVERY_WAIT_REASONS = frozenset({
    "read_budget_wait", "telegram_cooldown", "lease_busy", "lease_unavailable",
    "static_proxy_unavailable", "session_lease_required",
})


def normalized_phone(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    number = re.sub(r"[ ()-]", "", value.strip())
    if not re.fullmatch(r"\+?[1-9][0-9]{6,14}", number):
        return None
    return number.removeprefix("+")


def recovery_failure(error: BaseException) -> str:
    """Only fixed reason codes cross the audit/API boundary."""
    causes, current, chain = set(), error, []
    while current is not None and id(current) not in causes:
        causes.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    # Authorization/risk failures take precedence over a wrapping transport
    # exception. A timeout must never make a revoked session auto-retryable.
    for current in chain:
        name = type(current).__name__
        if name in {"AuthKeyUnregisteredError", "AuthKeyNotFound", "SessionRevokedError", "SessionExpiredError"}:
            return "session_reauthentication_required"
        if name in {"UserDeactivatedError", "UserDeactivatedBanError", "PhoneNumberBannedError"}:
            return "account_deactivated"
        if name == "FrozenMethodInvalidError":
            return "account_frozen"
        if name == "RuntimeError" and "not authorized" in str(current).lower():
            return "session_reauthentication_required"
        if isinstance(current, RecoveryCheckFailed) and str(current) in {
            "identity_mismatch", "identity_unavailable", "session_unauthorized", "account_frozen",
        }:
            return ("session_reauthentication_required" if str(current) == "session_unauthorized"
                    else str(current))
    for current in chain:
        name = type(current).__name__
        if name in {"AccountOperationLeaseBusy", "AccountSessionLeaseBusy"}:
            return "lease_busy"
        if name in {"AccountOperationLeaseUnavailable", "QuarantineUnavailable"}:
            return "lease_unavailable"
        if name == "RpcDeferred":
            return "read_budget_wait"
        if name == "FloodWaitError":
            return "telegram_cooldown"
        if isinstance(current, RecoveryCheckFailed):
            if str(current) in {"session_lease_required", "operation_lease_lost", "cleanup_failed",
                               "probe_read_limit"}:
                return str(current)
    if any(type(item).__name__ in {"AuthKeyDuplicatedError", "SessionQuarantined"} for item in chain):
        return "session_quarantined"
    if any(isinstance(item, FileNotFoundError) for item in chain):
        return "session_missing"
    if any(type(item).__name__ in {"StaticProxyUnavailableError", "ProxyConnectionError",
                                  "ProxyTimeoutError", "ProxyError", "GeneralProxyError",
                                  "SOCKS5Error", "SOCKS4Error"} for item in chain):
        return "proxy_unavailable"
    if any(isinstance(item, (TimeoutError, asyncio.TimeoutError)) for item in chain):
        return "connection_timeout"
    network_errors = {errno.ECONNABORTED, errno.ECONNREFUSED, errno.ECONNRESET, errno.EHOSTUNREACH,
                      errno.ENETDOWN, errno.ENETUNREACH, errno.EPIPE, errno.ETIMEDOUT}
    if any(isinstance(item, (ConnectionError, socket.gaierror))
           or (isinstance(item, OSError) and item.errno in network_errors) for item in chain):
        return "network_unavailable"
    return "verification_failed"


def connection_failure_reason(error: BaseException) -> str:
    """Classify trusted transport failures without persisting exception text."""
    reason = recovery_failure(error)
    if reason == "session_quarantined":
        return "session_conflict"
    if reason == "verification_failed":
        from app.core.account.session_guard import is_wrong_session_error

        if is_wrong_session_error(error):
            return "session_conflict"
    return reason


def recovery_result(error: BaseException) -> RecoveryResult:
    reason = recovery_failure(error)
    delay = None
    current, seen = error, set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ in {"RpcDeferred", "FloodWaitError"}:
            value = (getattr(current, "retry_after_seconds", None)
                     or getattr(current, "retry_after", None) or getattr(current, "seconds", None))
            if isinstance(value, (int, float)) and value > 0:
                delay = min(86400 * 30, max(1, int(value)))
                break
        current = current.__cause__ or current.__context__
    return RecoveryResult(False, reason, delay)


def _snapshot(account: TelegramAccount, proxy: Proxy) -> str:
    # The digest is used only in memory to fence credential/environment changes.
    data = {key: getattr(account, key, None) for key in (
        "phone", "session_name", "session_string", "auth_key_base64", "api_config_id",
        "proxy_mode", "static_proxy_id", "fingerprint_id", "device_model", "system_version",
        "app_version", "updated_at", "risk_level", "risk_pause_until", "is_active",
        "restriction_source", "spam_check_status",
    )}
    data["api"] = tuple(getattr(getattr(account, "api_config", None), key, None)
                        for key in ("api_id", "api_hash"))
    data["proxy"] = tuple(getattr(proxy, key, None)
                          for key in ("host", "port", "protocol", "username", "password", "is_active"))
    config = getattr(account, "operation_config", None)
    data["operations"] = (getattr(config, "enabled", False), getattr(config, "updated_at", None))
    return hashlib.sha256(json.dumps(data, default=str, sort_keys=True).encode()).hexdigest()


class AccountRecoveryService:
    def __init__(self, db: AsyncSession, pool: AccountPool) -> None:
        self.db, self.pool = db, pool
        self._source = "explicit_error_recovery"
        self._attempt_key: str | None = None
        self._attempt_token: str | None = None
        self._expected_failure_id: int | None = None
        self._expected_error_updated_at: str | None = None

    async def _eligible(self, account: TelegramAccount | None) -> tuple[str | None, Proxy | None]:
        if account is None:
            return "account_missing", None
        if account.status != AccountStatus.ERROR:
            return "account_not_error", None
        if self._source == "automatic_error_recovery":
            if not bool(getattr(getattr(account, "operation_config", None), "enabled", False)):
                return "operation_disabled", None
            if (self._expected_failure_id is None or self._expected_error_updated_at is None
                    or account.updated_at.isoformat() != self._expected_error_updated_at):
                return "recovery_episode_changed", None
            event = await self.db.scalar(select(AccountEnvironmentEvent).where(
                AccountEnvironmentEvent.account_id == account.id,
                AccountEnvironmentEvent.event_type == "account_connection_failure",
            ).order_by(AccountEnvironmentEvent.id.desc()).limit(1))
            if event is None or event.id != self._expected_failure_id:
                return "recovery_episode_changed", None
            try:
                details = json.loads(event.details or "{}")
            except (TypeError, ValueError):
                return "recovery_episode_changed", None
            if (not isinstance(details, dict) or details.get("source") != "account_pool"
                    or details.get("error_updated_at") != self._expected_error_updated_at
                    or event.status != "failed" or event.reason not in RECOVERY_RETRYABLE_REASONS):
                return "recovery_episode_changed", None
            latest = await self.db.scalar(select(AccountEnvironmentEvent).where(
                AccountEnvironmentEvent.account_id == account.id,
                AccountEnvironmentEvent.event_type == "session_recovery",
                AccountEnvironmentEvent.id > event.id,
            ).order_by(AccountEnvironmentEvent.id.desc()).limit(1))
            if latest is not None:
                retryable = RECOVERY_RETRYABLE_REASONS | RECOVERY_WAIT_REASONS
                try:
                    latest_details = json.loads(latest.details or "{}")
                except (TypeError, ValueError):
                    latest_details = {}
                cancelled_auto = (isinstance(latest_details, dict) and latest.reason == "verification_cancelled"
                                  and latest_details.get("source") == "automatic_error_recovery")
                if latest.status == "ok" or (latest.reason not in retryable and not cancelled_auto):
                    return "recovery_episode_changed", None
        if (not account.is_active or account.risk_level not in {"normal", "watch"}
                or (account.risk_pause_until and account.risk_pause_until > datetime.utcnow())
                or account.restriction_source or account.spam_check_status != "clear"):
            return "account_risk_blocked", None
        if normalized_phone(account.phone) is None:
            return "identity_unavailable", None
        mode = getattr(account.proxy_mode, "value", account.proxy_mode)
        if mode != "static" or not account.static_proxy_id:
            return "static_proxy_required", None
        proxy = await self.db.get(Proxy, account.static_proxy_id, populate_existing=True)
        if proxy is None or not proxy.is_active or proxy.consecutive_failures >= 3:
            return "static_proxy_unavailable", None
        shared = await self.db.scalar(select(func.count()).select_from(TelegramAccount).where(
            TelegramAccount.static_proxy_id == account.static_proxy_id,
            TelegramAccount.is_active.is_(True), TelegramAccount.id != account.id,
        ))
        if shared:
            return "static_proxy_shared", None
        return None, proxy

    def _audit(self, account_id: int, result: RecoveryResult) -> None:
        self.db.add(AccountEnvironmentEvent(
            account_id=account_id, event_type="session_recovery",
            status="ok" if result.ok else "failed", reason=result.reason,
            details=json.dumps({"source": self._source, "attempt_limit": 1,
                                "identity_verified": result.ok, "marketing_writes": 0}),
        ))

    async def recover(
        self, account_id: int, *, source: str = "explicit_error_recovery",
        expected_failure_id: int | None = None, expected_error_updated_at: str | None = None,
    ) -> RecoveryResult:
        """Serialize manual and automatic probes before touching the session fence.

        A bounded wall time stays below the Redis lease lifetime. Expired or
        replaced owners cannot commit ONLINE, even after a slow dependency.
        """
        if source not in {"explicit_error_recovery", "automatic_error_recovery"}:
            return RecoveryResult(False, "recovery_source_invalid")
        from app.core.redis import redis_client

        if redis_client is None:
            return RecoveryResult(False, "lease_unavailable")
        self._source = source
        self._expected_failure_id = expected_failure_id
        self._expected_error_updated_at = expected_error_updated_at
        self._attempt_key = f"vanguard:account:recovery-probe:{account_id}"
        self._attempt_token = secrets.token_hex(16)
        # No Redis wait may retain a caller's database read transaction.
        await self.db.rollback()
        try:
            async with asyncio.timeout(3):
                claimed = await redis_client.set(self._attempt_key, self._attempt_token, nx=True, ex=180)
        except Exception:
            return RecoveryResult(False, "lease_unavailable")
        if not claimed:
            return RecoveryResult(False, "lease_busy")
        try:
            async with asyncio.timeout(100):
                return await self._recover_once(account_id)
        except TimeoutError:
            await self.db.rollback()
            result = RecoveryResult(False, "connection_timeout")
            self._audit(account_id, result)
            await self.db.commit()
            return result
        finally:
            await self.db.rollback()
            try:
                async with asyncio.timeout(3):
                    await redis_client.eval(
                        "if redis.call('get', KEYS[1]) == ARGV[1] then "
                        "return redis.call('del', KEYS[1]) end return 0",
                        1, self._attempt_key, self._attempt_token,
                    )
            except Exception:
                pass  # A stale lease expires; never delete another owner.

    async def _recover_once(self, account_id: int) -> RecoveryResult:
        account = await AccountManager(self.db).get_account(account_id)
        reason, proxy = await self._eligible(account)
        if reason:
            result = RecoveryResult(False, reason)
            if account is not None:
                self._audit(account_id, result)
                await self.db.commit()
            return result
        before, phone, proxy_id = _snapshot(account, proxy), account.phone, account.static_proxy_id
        quarantine_token = None
        try:
            # All selectin-loaded credentials/configuration become an immutable
            # detached snapshot. Pool admission may wait on Redis/proxy I/O.
            self.db.expunge_all()
            await self.db.rollback()
            await self.pool.add_account_from_db(account)
            from app.core.account.session_quarantine import current, publish
            from app.core.redis import redis_client

            quarantine_token = await current(account_id, redis_client)
            # Every explicit recovery has a generation, including ordinary
            # ERROR rows that did not previously have a session fence.
            published = await publish(account_id, redis_client, token=quarantine_token)
            quarantine_token = published.token
            if not published.sentinel_confirmed:
                raise RecoveryCheckFailed("session_lease_required")
            result = await self.pool.verify_error_account(
                account_id, expected_phone=phone, expected_proxy_id=proxy_id,
                quarantine_token=quarantine_token,
            )
        except asyncio.CancelledError:
            # Cancellation never publishes ONLINE. The pool owns probe cleanup.
            await self.db.rollback()
            self._audit(account_id, RecoveryResult(False, "verification_cancelled"))
            await self.db.commit()
            raise
        except Exception as error:
            result = recovery_result(error)
        if not result.ok:
            self._audit(account_id, result)
            await self.db.commit()
            return result
        from app.core.account.session_quarantine import (
            DB_TIMEOUT_SECONDS,
            persist_error,
            recovery_scope,
        )

        # Serialize final commit/clear with token-checked ERROR retries. Redis
        # validation occurs before opening a DB TX, clear occurs after rollback.
        try:
            async with recovery_scope(account_id, quarantine_token, redis_client) as clear_fence:
                current_attempt = await redis_client.get(self._attempt_key)
                if isinstance(current_attempt, bytes):
                    current_attempt = current_attempt.decode("ascii")
                if current_attempt != self._attempt_token:
                    raise RecoveryCheckFailed("operation_lease_lost")
                async with asyncio.timeout(DB_TIMEOUT_SECONDS):
                    account = await self.db.scalar(select(TelegramAccount).where(
                        TelegramAccount.id == account_id,
                    ).options(selectinload(TelegramAccount.operation_config))
                    .with_for_update(of=TelegramAccount).execution_options(populate_existing=True))
                    # The operation switch lives in a separate row. Lock and
                    # refresh it so disabling during verification wins.
                    await self.db.scalar(select(AccountOperationConfig).where(
                        AccountOperationConfig.account_id == account_id,
                    ).with_for_update(of=AccountOperationConfig).execution_options(populate_existing=True))
                    reason, proxy = await self._eligible(account)
                    if reason or _snapshot(account, proxy) != before:
                        result = RecoveryResult(False, "account_changed_during_verification")
                        self._audit(account_id, result)
                        await self.db.commit()
                        return result
                    session_name = account.session_name
                    self._audit(account_id, result)
                    await AccountManager(self.db).update_account_status(account_id, AccountStatus.ONLINE)
                    # The manager refreshes after commit; close that read TX.
                    await self.db.rollback()
                if not await clear_fence():
                    raise RecoveryCheckFailed("session_quarantined")
        except BaseException as error:
            await self.db.rollback()
            if quarantine_token is not None:
                await persist_error(account_id, token=quarantine_token, redis=redis_client)
            if isinstance(error, asyncio.CancelledError):
                raise
            result = RecoveryResult(False, "session_quarantined")
            self._audit(account_id, result)
            await self.db.commit()
            return result
        # Discard only this disconnected probe wrapper. A normal owner reloads
        # the committed account; stale local quarantine flags are never reused.
        await self.pool.remove_account(session_name)
        return result
