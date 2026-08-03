"""Environment audit and consistency checks for Telegram accounts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.models import AccountEnvironmentEvent, TelegramAccount


@dataclass(frozen=True)
class EnvironmentSnapshot:
    proxy_mode: Optional[str] = None
    proxy_id: Optional[int] = None
    proxy_country: Optional[str] = None
    fingerprint_id: Optional[str] = None
    device_model: Optional[str] = None
    system_version: Optional[str] = None
    app_version: Optional[str] = None


class AccountEnvironmentGuard:
    """Records environment changes and flags incompatible account runtime contexts."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def record_event(
        self,
        account: TelegramAccount | Any,
        event_type: str,
        *,
        snapshot: Optional[EnvironmentSnapshot] = None,
        status: str = "ok",
        reason: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> AccountEnvironmentEvent:
        snapshot = snapshot or self.snapshot_from_account(account)
        event = AccountEnvironmentEvent(
            account_id=getattr(account, "id", None) or getattr(account, "account_id", None),
            event_type=event_type,
            status=status,
            reason=reason,
            proxy_mode=snapshot.proxy_mode,
            proxy_id=snapshot.proxy_id,
            proxy_country=snapshot.proxy_country,
            fingerprint_id=snapshot.fingerprint_id,
            device_model=snapshot.device_model,
            system_version=snapshot.system_version,
            app_version=snapshot.app_version,
            details=json.dumps(details, ensure_ascii=False, default=str) if details else None,
        )
        self.db.add(event)
        await self.db.commit()
        return event

    async def validate_runtime_environment(self, account: TelegramAccount | Any) -> tuple[bool, Optional[str]]:
        return self.validate_account_environment(account)

    @staticmethod
    def validate_account_environment(account: TelegramAccount | Any) -> tuple[bool, Optional[str]]:
        preferred_country = (getattr(account, "preferred_country", None) or getattr(account, "country_code", None) or "").upper()
        runtime_country = (getattr(account, "current_proxy_country", None) or getattr(account, "country_code", None) or "").upper()
        if preferred_country and runtime_country and preferred_country != runtime_country:
            return False, "proxy_country_mismatch"
        if not getattr(account, "fingerprint_id", None):
            return False, "fingerprint_missing"
        if not getattr(account, "device_model", None):
            return False, "device_model_missing"
        if not getattr(account, "system_version", None):
            return False, "system_version_missing"
        if not getattr(account, "app_version", None):
            return False, "app_version_missing"
        return True, None

    @staticmethod
    def snapshot_from_account(account: TelegramAccount | Any) -> EnvironmentSnapshot:
        return EnvironmentSnapshot(
            proxy_mode=getattr(getattr(account, "proxy_mode", None), "value", getattr(account, "proxy_mode", None)),
            proxy_id=getattr(account, "static_proxy_id", None),
            proxy_country=(getattr(account, "current_proxy_country", None) or getattr(account, "country_code", None)),
            fingerprint_id=getattr(account, "fingerprint_id", None),
            device_model=getattr(account, "device_model", None),
            system_version=getattr(account, "system_version", None),
            app_version=getattr(account, "app_version", None),
        )

