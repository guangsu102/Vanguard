"""Fail-closed coverage of managed human Telegram identities for ad evidence."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select

from app.core.account.models import AccountType, TelegramAccount


def parse_identity_map(value: Any) -> dict[int, int] | None:
    """The setting maps local account IDs to numeric Telegram user IDs."""
    if not isinstance(value, dict) or not value:
        return None
    result: dict[int, int] = {}
    for key, user_id in value.items():
        if not isinstance(key, str) or not key.isdecimal() or int(key) <= 0:
            return None
        if type(user_id) is not int or user_id <= 0:
            return None
        account_id = int(key)
        if account_id in result:
            return None
        result[account_id] = user_id
    if len(set(result.values())) != len(result):
        return None
    return result


async def covered_system_ids(
    db: Any, config: dict[str, Any], *, account_id: int, live_user_id: int | None = None
) -> tuple[set[int], str | None]:
    """Require a distinct identity for every managed promoter, including inactive ones.

    The live account is checked against Telegram's get_me result when supplied.
    Unmapped or newly registered accounts invalidate prior coverage immediately.
    Legacy system_user_ids remain exclusions but cannot establish completeness.
    """
    mapping = parse_identity_map(config.get("system_account_user_ids"))
    if mapping is None:
        return set(), None
    managed = set(
        (
            await db.scalars(
                select(TelegramAccount.id).where(
                    TelegramAccount.account_type == AccountType.PROMOTER
                )
            )
        ).all()
    )
    if not managed or account_id not in managed or not managed <= mapping.keys():
        return set(), None
    if live_user_id is not None and mapping[account_id] != live_user_id:
        return set(), None
    extras = config.get("system_user_ids", [])
    if not isinstance(extras, list) or any(type(value) is not int or value <= 0 for value in extras):
        return set(), None
    ids = set(mapping.values()) | set(extras)
    fingerprint = hashlib.sha256(
        json.dumps(
            {"managed": sorted(managed), "mapping": sorted(mapping.items()), "extras": sorted(extras)},
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return ids, fingerprint


async def ensure_system_identities(service: Any, *, limit: int = 2) -> dict[str, int]:
    """Register missing managed identities from authenticated get_me reads.

    The account lease covers the read. The settings row is locked only after I/O;
    concurrent registrars merge instead of replacing each other's mappings.
    """
    import asyncio
    from datetime import datetime, timedelta
    from sqlalchemy import update, func
    from app.core.settings_models import SystemSetting
    from app.modules.acquisition.models import GroupQualificationAudit
    from app.modules.acquisition.group_qualification import POLICY_VERSION

    key = "automation.group_qualification"
    record = await service.db.get(SystemSetting, key, populate_existing=True)
    config = json.loads(record.value) if record else {}
    result = {"registered": 0, "deferred": 0}
    if not config.get("enabled") or not config.get("automatic_identity_registration"):
        return result
    mapping = parse_identity_map(config.get("system_account_user_ids"))
    if mapping is None and config.get("system_account_user_ids"):
        return {**result, "deferred": 1}
    mapping = mapping or {}
    accounts = (await service.db.scalars(select(TelegramAccount).where(
        TelegramAccount.account_type == AccountType.PROMOTER,
        TelegramAccount.id.not_in(mapping),
    ).order_by(TelegramAccount.id))).all()
    # Sessionless drafts cannot supply get_me; retry them after credentials arrive.
    processed = 0
    for account in accounts:
        if processed >= limit:
            break
        # Rotate inaccessible accounts using a durable retry time, so the first
        # broken session cannot starve later newly registered accounts.
        await service.db.scalar(select(SystemSetting).where(SystemSetting.key == key).with_for_update())
        retry_key = f"qualification.identity.retry.{account.id}"
        retry = await service.db.get(SystemSetting, retry_key, populate_existing=True)
        due = datetime.fromisoformat(retry.value) if retry else datetime.min
        if due > datetime.utcnow():
            await service.db.commit()
            continue
        next_check = (datetime.utcnow() + timedelta(minutes=15)).isoformat()
        if retry is None:
            service.db.add(SystemSetting(key=retry_key, value=next_check))
        else:
            retry.value = next_check
        await service.db.commit()
        processed += 1
        await service.account_pool.add_account_from_db(account)
        wrapper = None
        try:
            wrapper = await service.account_pool.acquire_by_id(
                account.id, purpose="qualification_identity_registration",
                raise_on_lease_failure=True, allow_restricted=True,
            )
            if wrapper is None or wrapper.client is None:
                result["deferred"] += 1
                continue
            me = await asyncio.wait_for(wrapper.client.get_me(), timeout=15)
            user_id = getattr(me, "id", None)
            if type(user_id) is not int or user_id <= 0 or getattr(me, "bot", False):
                result["deferred"] += 1
                continue
        except Exception:
            result["deferred"] += 1
            continue
        finally:
            if wrapper is not None:
                await service.account_pool.release(wrapper)
        record = await service.db.scalar(select(SystemSetting).where(
            SystemSetting.key == key,
        ).with_for_update().execution_options(populate_existing=True))
        config = json.loads(record.value) if record else {}
        if not config.get("enabled") or not config.get("automatic_identity_registration"):
            await service.db.rollback()
            break
        current = parse_identity_map(config.get("system_account_user_ids"))
        if current is None and config.get("system_account_user_ids"):
            await service.db.rollback()
            break
        current = current or {}
        if account.id in current or user_id in current.values():
            await service.db.rollback()
            result["deferred"] += 1
            continue
        current[account.id] = user_id
        config["system_account_user_ids"] = {str(k): v for k, v in current.items()}
        record.value = json.dumps(config, ensure_ascii=False)
        now = datetime.utcnow()
        # A changed exclusion set invalidates captured proof. Renew automatically,
        # so pre-existing approvals never wait another 23 hours for the new map.
        await service.db.execute(update(GroupQualificationAudit).where(
            GroupQualificationAudit.policy_version == POLICY_VERSION,
            GroupQualificationAudit.id.in_(select(func.max(GroupQualificationAudit.id)).where(
                GroupQualificationAudit.state != "cancelled"
            ).group_by(GroupQualificationAudit.membership_id)),
            GroupQualificationAudit.state == "completed",
            GroupQualificationAudit.decision.in_(["allowed", "trial"]),
        ).values(next_retry_at=now, expires_at=now))
        await service.db.commit()
        result["registered"] += 1
    return result
