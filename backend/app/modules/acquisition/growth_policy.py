"""Growth business controls, independent of historical qualification evidence.

The old settings key remains stable for API compatibility and operator pauses.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.core.account.models import TelegramAccount
from app.core.group.identity import telegram_group_id_aliases
from app.core.group.models import Group, GroupAccountMembership
from app.core.settings_models import SystemSetting

SETTING_KEY = "automation.group_qualification"

URL = re.compile(
    r"(?:https?://|t\.me/|telegram\.me/|www\.|(?<![\w@])(?:[a-z0-9-]+\.)+[a-z]{2,}\b)[^\s<>]*",
    re.I,
)


def _ids(values: Any) -> set[int]:
    return {int(value) for value in (values or []) if str(value).lstrip("-").isdigit()}


async def policy(db: Any, *, fresh: bool = False) -> dict[str, Any]:
    row = (
        await db.get(SystemSetting, SETTING_KEY, populate_existing=True)
        if fresh
        else await db.get(SystemSetting, SETTING_KEY)
    )
    config = json.loads(row.value) if row and row.value else {"enabled": False, "execute_exits": False}
    from app.core.config import settings
    if settings.TELEGRAM_RPC_PACING_ENABLED:
        from app.core.account.models import AccountOperationConfig, AccountType
        active = list((await db.scalars(select(TelegramAccount.id).join(
            AccountOperationConfig, AccountOperationConfig.account_id == TelegramAccount.id).where(
                TelegramAccount.account_type == AccountType.PROMOTER,
                AccountOperationConfig.enabled.is_(True)))).all())
        config["account_ids"] = active
        config["verification_account_ids"] = active
        config["promotion_account_ids"] = active
        rollout = config.setdefault("rollout_accounts", {})
        for account_id in active:
            rollout.setdefault(str(account_id), {"phase": "dynamic"})
    if config.get("verification_active_promoters") is True:
        from app.core.account.models import AccountOperationConfig, AccountType
        config["verification_account_ids"] = list((await db.scalars(
            select(AccountOperationConfig.account_id).join(
                TelegramAccount, TelegramAccount.id == AccountOperationConfig.account_id
            ).where(
                AccountOperationConfig.enabled.is_(True),
                AccountOperationConfig.auto_join_enabled.is_(True),
                TelegramAccount.account_type == AccountType.PROMOTER,
            )
        )).all())
    if config.get("promotion_active_promoters") is True:
        from app.core.account.models import AccountOperationConfig, AccountType
        active = list((await db.scalars(select(AccountOperationConfig.account_id).join(
            TelegramAccount, TelegramAccount.id == AccountOperationConfig.account_id,
        ).where(
            AccountOperationConfig.enabled.is_(True),
            AccountOperationConfig.auto_ads_enabled.is_(True),
            TelegramAccount.account_type == AccountType.PROMOTER,
        ))).all())
        config["promotion_account_ids"] = active
        rollout = config.setdefault("rollout_accounts", {})
        for account_id in active:
            # Explicit pauses remain pauses; a newly enabled promoter inherits
            # the global rules without requiring a second static allowlist edit.
            rollout.setdefault(str(account_id), {"phase": "dynamic"})
    return config


def manually_protected(
    config: dict[str, Any], group: Group, membership: GroupAccountMembership | None
) -> bool:
    """Manual protection is separate from permission to advertise."""
    return (
        group.id in _ids(config.get("manual_protected_group_ids"))
        or bool(
            set(telegram_group_id_aliases(group.group_id))
            & _ids(config.get("protected_telegram_group_ids"))
        )
        or (
            membership is not None and membership.id in _ids(config.get("protected_membership_ids"))
        )
    )


def account_block_reason(account: TelegramAccount | None, now: datetime) -> str | None:
    if account is None or not account.is_active:
        return "account_unavailable"
    status = str(getattr(account.status, "value", account.status)).lower()
    if status in {"banned", "restricted", "error"}:
        return "account_status_unavailable"
    if account.spam_check_status in {"restricted", "flagged"}:
        return "account_spam_restricted"
    if account.risk_pause_until and account.risk_pause_until > now:
        return "account_risk_paused"
    if account.risk_level not in {"normal", "watch"}:
        return "account_risk_level_not_allowed"
    return None
