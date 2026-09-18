"""Cross-process invalidation for Telegram account proxy policy changes."""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.models import ProxyMode, TelegramAccount

ACCOUNT_PROXY_POLICY_CHANNEL = "vanguard:account:proxy-policy"
ACCOUNT_PROXY_POLICY_STATE_PREFIX = "vanguard:account:proxy-policy:state"
ACCOUNT_PROXY_POLICY_VERSION_PREFIX = "vanguard:account:proxy-policy:version"

logger = structlog.get_logger()
_listener_task: asyncio.Task | None = None


@dataclass(frozen=True)
class ProxyPolicyState:
    account_id: int
    version: int
    proxy_mode: str
    static_proxy_id: int | None


def _state_key(account_id: int) -> str:
    return f"{ACCOUNT_PROXY_POLICY_STATE_PREFIX}:{account_id}"


def _version_key(account_id: int) -> str:
    return f"{ACCOUNT_PROXY_POLICY_VERSION_PREFIX}:{account_id}"


async def get_bound_static_accounts(
    db: AsyncSession,
    proxy_id: int,
    *,
    populate_existing: bool = False,
) -> list[TelegramAccount]:
    """Return accounts whose persisted policy still references a static proxy."""
    query = select(TelegramAccount).where(TelegramAccount.static_proxy_id == proxy_id)
    if populate_existing:
        query = query.execution_options(populate_existing=True)
    result = await db.execute(query)
    return list(result.scalars().all())


async def get_accounts_by_ids(
    db: AsyncSession,
    account_ids: list[int],
    *,
    populate_existing: bool = False,
) -> list[TelegramAccount]:
    """Reload affected accounts by ID after a proxy mutation commits."""
    if not account_ids:
        return []
    query = select(TelegramAccount).where(TelegramAccount.id.in_(account_ids))
    if populate_existing:
        query = query.execution_options(populate_existing=True)
    result = await db.execute(query)
    accounts_by_id = {account.id: account for account in result.scalars().all()}
    return [accounts_by_id[account_id] for account_id in account_ids if account_id in accounts_by_id]


async def refresh_bound_static_accounts(
    accounts: list[TelegramAccount],
    *,
    reason: str,
) -> int:
    """Evict local clients and publish a policy generation for every account."""
    from app.core.account.pool import invalidate_account_in_all_pools

    failures: list[Exception] = []
    for account in accounts:
        try:
            await invalidate_account_in_all_pools(account.id, reason=reason)
        except Exception as exc:
            failures.append(exc)
            logger.warning(
                "static_proxy_bound_account_local_invalidation_failed",
                account_id=account.id,
                reason=reason,
                error=str(exc),
            )

        proxy_mode = getattr(account.proxy_mode, "value", str(account.proxy_mode))
        static_proxy_id = (
            account.static_proxy_id if proxy_mode == ProxyMode.STATIC.value else None
        )
        try:
            await publish_account_proxy_policy_changed(
                account.id,
                proxy_mode,
                static_proxy_id,
            )
        except Exception as exc:
            failures.append(exc)
            logger.warning(
                "static_proxy_bound_account_policy_publish_failed",
                account_id=account.id,
                reason=reason,
                error=str(exc),
            )

    if failures:
        raise RuntimeError(
            f"Failed to refresh {len(failures)} static proxy account operations"
        ) from failures[0]
    return len(accounts)


async def refresh_static_proxy_bindings(
    db: AsyncSession,
    proxy_id: int,
    *,
    reason: str,
) -> int:
    """Propagate a static proxy availability change to all bound accounts."""
    accounts = await get_bound_static_accounts(db, proxy_id)
    return await refresh_bound_static_accounts(accounts, reason=reason)


def _decode_state(raw: object) -> ProxyPolicyState | None:
    if not raw:
        return None
    try:
        payload = json.loads(str(raw))
        static_proxy_id = payload.get("static_proxy_id")
        return ProxyPolicyState(
            account_id=int(payload["account_id"]),
            version=int(payload["version"]),
            proxy_mode=str(payload["proxy_mode"]),
            static_proxy_id=int(static_proxy_id) if static_proxy_id is not None else None,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        logger.warning("account_proxy_policy_state_invalid")
        return None


async def get_account_proxy_policy_state(account_id: int) -> ProxyPolicyState | None:
    """Return the durable Redis policy state when Redis is available."""
    from app.core.redis import get_redis

    try:
        client = await get_redis()
        return _decode_state(await client.get(_state_key(account_id)))
    except RuntimeError:
        return None
    except Exception as exc:
        logger.warning(
            "account_proxy_policy_state_read_failed",
            account_id=account_id,
            error=str(exc),
        )
        return None


async def publish_account_proxy_policy_changed(
    account_id: int,
    proxy_mode: str,
    static_proxy_id: int | None,
) -> ProxyPolicyState:
    """Persist and broadcast a proxy policy generation after the DB commit."""
    from app.core.redis import get_redis

    client = await get_redis()
    version = int(await client.incr(_version_key(account_id)))
    state = ProxyPolicyState(
        account_id=account_id,
        version=version,
        proxy_mode=proxy_mode,
        static_proxy_id=static_proxy_id,
    )
    payload = json.dumps(
        {
            "account_id": state.account_id,
            "version": state.version,
            "proxy_mode": state.proxy_mode,
            "static_proxy_id": state.static_proxy_id,
        },
        separators=(",", ":"),
    )
    await client.set(_state_key(account_id), payload)
    await client.publish(ACCOUNT_PROXY_POLICY_CHANNEL, payload)
    logger.info(
        "account_proxy_policy_changed_published",
        account_id=account_id,
        version=version,
        proxy_mode=proxy_mode,
        static_proxy_id=static_proxy_id,
    )
    return state


async def _proxy_policy_listener_loop() -> None:
    """Disconnect stale account clients in this process as events arrive."""
    from app.core.redis import get_redis

    while True:
        pubsub = None
        try:
            client = await get_redis()
            pubsub = client.pubsub()
            await pubsub.subscribe(ACCOUNT_PROXY_POLICY_CHANNEL)
            async for item in pubsub.listen():
                if item.get("type") != "message":
                    continue
                state = _decode_state(item.get("data"))
                if state is None:
                    continue
                from app.core.account.pool import invalidate_account_in_all_pools

                await invalidate_account_in_all_pools(
                    state.account_id,
                    reason=f"proxy_policy_event:{state.version}",
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("account_proxy_policy_listener_failed", error=str(exc))
            await asyncio.sleep(2)
        finally:
            if pubsub is not None:
                with contextlib.suppress(Exception):
                    await pubsub.aclose()


async def start_account_proxy_policy_listener() -> None:
    global _listener_task
    if _listener_task is None or _listener_task.done():
        _listener_task = asyncio.create_task(
            _proxy_policy_listener_loop(),
            name="account-proxy-policy-listener",
        )


async def stop_account_proxy_policy_listener() -> None:
    global _listener_task
    if _listener_task is None:
        return
    _listener_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _listener_task
    _listener_task = None
