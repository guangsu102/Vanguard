"""
Leave C-level joined groups and mark them inactive locally.

The business rule is that Vanguard only keeps A/B groups in the active pool.
C-level groups must be exited from Telegram with the owning account, then
marked left/rejected in the local database.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.account.models import AccountStatus, AccountType, TelegramAccount
from app.core.account.pool import close_account_pool, init_account_pool
from app.core.config import settings
from app.core.database import close_db, get_db_session, init_db
from app.core.group.models import Group, GroupAccountMembership, GroupLevel
from app.modules.acquisition.automation import AcquisitionAutomationService


def _now() -> datetime:
    return datetime.utcnow()


LOCAL_CLEANUP_ERRORS = {
    "entity_not_in_dialogs",
    "entity_not_joinable_group",
    "entity_not_found",
}


async def main() -> None:
    await init_db(create_tables=False)
    pool = await init_account_pool(
        strategy="least_used",
        evomi_api_key=getattr(settings, "EVOMI_API_KEY", None),
        decodo_api_key=getattr(settings, "DECODO_API_KEY", None),
    )
    try:
        async with get_db_session() as db:
            account_rows = await db.execute(
                select(TelegramAccount).where(
                    TelegramAccount.account_type == AccountType.PROMOTER,
                    TelegramAccount.is_active == True,
                    TelegramAccount.status.notin_([AccountStatus.BANNED, AccountStatus.ERROR]),
                )
            )
            accounts = list(account_rows.scalars().all())
            await pool.sync_from_db(accounts)

            rows = await db.execute(
                select(GroupAccountMembership)
                .join(Group, Group.id == GroupAccountMembership.group_id)
                .options(
                    selectinload(GroupAccountMembership.group),
                    selectinload(GroupAccountMembership.account),
                )
                .where(
                    Group.level == GroupLevel.C,
                    GroupAccountMembership.status == "joined",
                )
                .order_by(GroupAccountMembership.id)
            )
            memberships = list(rows.scalars().all())
            service = AcquisitionAutomationService(db, account_pool=pool)

            left = 0
            failed = 0
            skipped = 0
            groups_rejected = 0
            details: list[dict] = []

            for membership in memberships:
                group = membership.group
                account = membership.account
                if group is None or account is None:
                    skipped += 1
                    continue
                if account.status in {AccountStatus.BANNED, AccountStatus.ERROR}:
                    skipped += 1
                    details.append(
                        {
                            "membership_id": membership.id,
                            "group_id": getattr(group, "id", None),
                            "telegram_group_id": getattr(group, "group_id", None),
                            "account_id": membership.account_id,
                            "status": "skipped",
                            "reason": f"account_status_{account.status.value}",
                        }
                    )
                    continue

                discovered = service._discovered_group_from_model(group)
                leave_error = await service._leave_group(membership.account_id, discovered)
                now = _now()
                locally_inactive = leave_error is None or leave_error in LOCAL_CLEANUP_ERRORS
                if locally_inactive:
                    membership.status = "left"
                    membership.left_at = now
                    membership.last_checked_at = now
                    membership.updated_at = now
                    membership.note = json.dumps(
                        {
                            "reason": "c_level_auto_leave" if leave_error is None else "c_level_local_cleanup",
                            "leave_error": leave_error,
                            "level": GroupLevel.C.value,
                            "level_score": float(group.level_score or 0),
                            "member_count": group.member_count,
                        },
                        ensure_ascii=False,
                    )[:4000]
                    group.status = "rejected"
                    group.updated_at = now
                    await db.commit()
                    left += 1
                    groups_rejected += 1
                    details.append(
                        {
                            "membership_id": membership.id,
                            "group_id": group.id,
                            "telegram_group_id": group.group_id,
                            "account_id": membership.account_id,
                            "status": "left" if leave_error is None else "local_cleanup",
                            "leave_error": leave_error,
                        }
                    )
                else:
                    membership.last_checked_at = now
                    membership.updated_at = now
                    membership.note = json.dumps(
                        {
                            "reason": "c_level_auto_leave_failed",
                            "error": leave_error,
                            "level": GroupLevel.C.value,
                            "level_score": float(group.level_score or 0),
                            "member_count": group.member_count,
                        },
                        ensure_ascii=False,
                    )[:4000]
                    await db.commit()
                    failed += 1
                    details.append(
                        {
                            "membership_id": membership.id,
                            "group_id": group.id,
                            "telegram_group_id": group.group_id,
                            "account_id": membership.account_id,
                            "status": "failed",
                            "error": leave_error,
                        }
                    )

            group_rows = await db.execute(
                select(Group)
                .where(
                    Group.level == GroupLevel.C,
                    Group.status == "active",
                )
                .order_by(Group.id)
            )
            for group in group_rows.scalars().all():
                if await service._has_joined_membership(group):
                    continue
                group.status = "rejected"
                group.updated_at = _now()
                groups_rejected += 1
            await db.commit()

            print(
                json.dumps(
                    {
                        "scanned": len(memberships),
                        "left": left,
                        "failed": failed,
                        "skipped": skipped,
                        "groups_rejected": groups_rejected,
                        "details": details[:50],
                    },
                    ensure_ascii=False,
                )
            )
    finally:
        await close_account_pool()
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
