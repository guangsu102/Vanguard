"""TrustSource dynamic proxy adapter.

The TrustSource allocation supplied for Vanguard is a fixed set of authenticated
residential gateway credentials.  The credentials are stored in the existing
``proxy`` inventory table, while account rows stay in ``dynamic`` mode.  This
adapter turns that inventory into the same ``ProxyInfo`` shape used by Evomi and
Decodo and selects a stable gateway for each account.

Keeping the selection in a provider adapter is important: a database proxy row
must never be interpreted as an account's static proxy binding.  Health checks
update the inventory's observed failure counters; unhealthy rows are skipped and
the next healthy row becomes the account's fallback.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

from sqlalchemy import select

from app.core.account.evomi import ProxyInfo
from app.core.account.exceptions import ProxyProviderError
from app.core.account.models import Proxy, ProxyMode, TelegramAccount

TRUSTSOURCE_PROVIDER = "trustsource"
TRUSTSOURCE_MAX_CONSECUTIVE_FAILURES = 3


class TrustSourceClient:
    """Resolve dynamic TrustSource proxies from the shared proxy inventory."""

    def __init__(
        self,
        session_factory: Callable[[], Any] | None = None,
        *,
        max_consecutive_failures: int = TRUSTSOURCE_MAX_CONSECUTIVE_FAILURES,
    ) -> None:
        # The default is imported lazily so unit tests and app startup can set up
        # the global SQLAlchemy engine before the adapter is first used.
        self._session_factory = session_factory
        self.max_consecutive_failures = max(1, int(max_consecutive_failures))

    def _get_session_factory(self) -> Callable[[], Any]:
        if self._session_factory is not None:
            return self._session_factory
        from app.core.database import get_db_session

        return get_db_session

    @staticmethod
    def _stable_start(account_key: str | None, size: int) -> int:
        if size <= 0:
            return 0
        seed = (account_key or "vanguard").encode("utf-8", errors="ignore")
        digest = hashlib.sha256(seed).digest()
        return int.from_bytes(digest[:8], "big") % size

    @staticmethod
    def _to_proxy_info(proxy: Proxy) -> ProxyInfo:
        if not proxy.username or not proxy.password:
            raise ValueError(f"TrustSource proxy {proxy.id} has no credentials")
        return ProxyInfo(
            protocol=str(proxy.protocol or "http").lower(),
            host=str(proxy.host),
            port=int(proxy.port),
            username=str(proxy.username),
            password=str(proxy.password),
            # A TrustSource row represents one dynamic session credential.  It
            # is sticky for the account during a connected session and can be
            # replaced by another healthy row after a health failure.
            session_id=f"trustsource-{proxy.id}",
            expires_at=None,
        )

    async def _list_healthy(self) -> list[Proxy]:
        session_factory = self._get_session_factory()
        async with session_factory() as db:
            result = await db.execute(
                select(Proxy)
                .where(
                    Proxy.provider.ilike(TRUSTSOURCE_PROVIDER),
                    Proxy.is_active.is_(True),
                    Proxy.consecutive_failures < self.max_consecutive_failures,
                )
                .order_by(Proxy.id)
            )
            rows = list(result.scalars().all())

        # Empty credentials are not usable as Telegram proxies.  Filter them
        # before deterministic assignment so one malformed row cannot consume a
        # stable account slot.
        return [row for row in rows if row.username and row.password]

    async def _dynamic_account_roster(self) -> list[tuple[int, str, str]]:
        """Return dynamic accounts and their stable provider-selection keys."""
        session_factory = self._get_session_factory()
        async with session_factory() as db:
            result = await db.execute(
                select(
                    TelegramAccount.id,
                    TelegramAccount.status,
                    TelegramAccount.phone,
                    TelegramAccount.session_name,
                ).where(
                    TelegramAccount.proxy_mode == ProxyMode.DYNAMIC,
                    TelegramAccount.static_proxy_id.is_(None),
                )
            )
            return [
                (
                    int(account_id),
                    getattr(status, "value", str(status)).lower(),
                    str(phone or session_name or account_id),
                )
                for account_id, status, phone, session_name in result.all()
            ]

    @classmethod
    def _exclusive_assignments(
        cls,
        rows: list[Proxy],
        roster: list[tuple[int, str] | tuple[int, str, str]],
        account_id: int,
        account_key: str | None = None,
    ) -> dict[int, Proxy]:
        """Assign one healthy provider credential to each dynamic account.

        The assignment is deterministic and process independent. Accounts that
        are currently online/idle/working get first claim on their stable-hash
        slot, so adding an idle account does not move a live session. Remaining
        accounts probe forward to the next unused row.
        """
        if not rows:
            return {}

        entries: dict[int, tuple[str, str]] = {}
        for item in roster:
            item_id = int(item[0])
            status = str(item[1]).lower()
            stable_key = str(item[2]) if len(item) >= 3 and item[2] else str(item_id)
            entries[item_id] = (status, stable_key)
        entries.setdefault(
            int(account_id),
            ("online", str(account_key or account_id)),
        )

        # Preserve every unique preferred slot for an online/working account.
        # Idle accounts are also allowed to keep a unique slot, but yield a
        # colliding slot to an account that is currently connected or working.
        def status_priority(status: str) -> int:
            if status in {"online", "working"}:
                return 0
            if status == "idle":
                return 1
            return 2

        preferred: dict[int, int] = {
            item_id: cls._stable_start(stable_key, len(rows))
            for item_id, (_status, stable_key) in entries.items()
        }
        by_preferred: dict[int, list[int]] = {}
        for item_id, slot in preferred.items():
            by_preferred.setdefault(slot, []).append(item_id)

        assignments: dict[int, Proxy] = {}
        occupied: set[int] = set()
        deferred: list[int] = []
        for slot, candidates in by_preferred.items():
            winner = min(
                candidates,
                key=lambda item_id: (
                    status_priority(entries[item_id][0]),
                    item_id,
                ),
            )
            assignments[winner] = rows[slot]
            occupied.add(int(rows[slot].id))
            deferred.extend(item_id for item_id in candidates if item_id != winner)

        # Only accounts that lost a collision probe forward. This keeps the
        # stable mapping of connected accounts unchanged when idle accounts are
        # added to the roster.
        for current_account_id in sorted(
            deferred,
            key=lambda item_id: (
                status_priority(entries[item_id][0]),
                preferred[item_id],
                item_id,
            ),
        ):
            start = preferred[current_account_id]
            for offset in range(len(rows)):
                candidate = rows[(start + offset) % len(rows)]
                if int(candidate.id) in occupied:
                    continue
                assignments[current_account_id] = candidate
                occupied.add(int(candidate.id))
                break
        return assignments

    async def get_proxy_for_account(
        self,
        country_code: str,
        count: int = 1,
        account_key: str | None = None,
        account_id: int | None = None,
    ) -> list[ProxyInfo]:
        """Return healthy TrustSource proxies for one account.

        Exact country rows are preferred.  The supplied ten-row inventory is
        US-routed, while some managed accounts retain CA/MM metadata; when no
        exact-country row exists, the healthy TrustSource inventory is used as a
        deliberate provider fallback so those accounts are not left without a
        proxy.  Assignment starts from a hash of the account key, which keeps a
        stable gateway per account without persisting a static binding.
        """

        requested = max(1, int(count))
        rows = await self._list_healthy()
        if not rows:
            raise ProxyProviderError(
                "TrustSource",
                "No healthy TrustSource proxy is available in the proxy inventory",
            )

        if account_id is not None:
            assignments = self._exclusive_assignments(
                rows,
                await self._dynamic_account_roster(),
                int(account_id),
                account_key=account_key,
            )
            assigned = assignments.get(int(account_id))
            if assigned is None:
                raise ProxyProviderError(
                    "TrustSource",
                    "No exclusive TrustSource proxy is available for this account",
                )
            selected = [assigned]
            if requested > 1:
                selected.extend(row for row in rows if row.id != assigned.id)
                selected = selected[:requested]
        else:
            country = (country_code or "").strip().upper()
            exact = [row for row in rows if str(row.country or "").upper() == country]
            candidates = exact or rows

            start = self._stable_start(account_key, len(candidates))
            selected = [
                candidates[(start + offset) % len(candidates)]
                for offset in range(min(requested, len(candidates)))
            ]
        try:
            return [self._to_proxy_info(row) for row in selected]
        except ValueError as exc:
            raise ProxyProviderError("TrustSource", str(exc)) from exc


_trustsource_client: TrustSourceClient | None = None


def get_trustsource_client() -> TrustSourceClient:
    """Return the process-local TrustSource adapter."""

    global _trustsource_client
    if _trustsource_client is None:
        _trustsource_client = TrustSourceClient()
    return _trustsource_client


def init_trustsource_client() -> TrustSourceClient:
    """Reset and return the process-local TrustSource adapter."""

    global _trustsource_client
    _trustsource_client = TrustSourceClient()
    return _trustsource_client


def close_trustsource_client() -> None:
    """Release the stateless process-local adapter."""

    global _trustsource_client
    _trustsource_client = None
