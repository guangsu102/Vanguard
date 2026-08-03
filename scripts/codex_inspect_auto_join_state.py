import asyncio
import json
from collections import Counter

from sqlalchemy import desc, func, select

from app.core.account.models import AccountOperationConfig, AccountType, TelegramAccount
from app.core.database import close_db, get_db_session, init_db
from app.modules.acquisition.models import AutoJoinAttempt, GroupSearchKeyword, SearchKeywordStatus


async def main() -> int:
    await init_db(create_tables=False)
    try:
        async with get_db_session() as db:
            account_rows = await db.execute(
                select(TelegramAccount.account_type, TelegramAccount.status, TelegramAccount.is_active, func.count(TelegramAccount.id))
                .group_by(TelegramAccount.account_type, TelegramAccount.status, TelegramAccount.is_active)
            )
            operation_rows = await db.execute(
                select(
                    AccountOperationConfig.enabled,
                    AccountOperationConfig.auto_join_enabled,
                    func.count(AccountOperationConfig.id),
                ).group_by(AccountOperationConfig.enabled, AccountOperationConfig.auto_join_enabled)
            )
            eligible_rows = await db.execute(
                select(TelegramAccount.id, TelegramAccount.identifier, TelegramAccount.status, TelegramAccount.is_active)
                .join(AccountOperationConfig, AccountOperationConfig.account_id == TelegramAccount.id)
                .where(
                    TelegramAccount.account_type == AccountType.PROMOTER,
                    TelegramAccount.is_active == True,
                    AccountOperationConfig.enabled == True,
                    AccountOperationConfig.auto_join_enabled == True,
                )
                .order_by(TelegramAccount.id)
                .limit(20)
            )
            keyword_rows = await db.execute(
                select(GroupSearchKeyword.status, GroupSearchKeyword.enabled, func.count(GroupSearchKeyword.id))
                .group_by(GroupSearchKeyword.status, GroupSearchKeyword.enabled)
            )
            approved_count = await db.execute(
                select(func.count(GroupSearchKeyword.id)).where(
                    GroupSearchKeyword.status == SearchKeywordStatus.APPROVED,
                    GroupSearchKeyword.enabled == True,
                )
            )
            attempts = await db.execute(
                select(AutoJoinAttempt)
                .order_by(desc(AutoJoinAttempt.attempted_at))
                .limit(20)
            )

            payload = {
                "accounts_by_type_status_active": [
                    {
                        "account_type": row[0].value if row[0] else None,
                        "status": row[1].value if hasattr(row[1], "value") else str(row[1]),
                        "is_active": row[2],
                        "count": row[3],
                    }
                    for row in account_rows.all()
                ],
                "operation_configs": [
                    {"enabled": row[0], "auto_join_enabled": row[1], "count": row[2]}
                    for row in operation_rows.all()
                ],
                "eligible_auto_join_accounts": [
                    {
                        "id": row[0],
                        "identifier": row[1],
                        "status": row[2].value if hasattr(row[2], "value") else str(row[2]),
                        "is_active": row[3],
                    }
                    for row in eligible_rows.all()
                ],
                "keywords_by_status_enabled": [
                    {
                        "status": row[0].value if hasattr(row[0], "value") else str(row[0]),
                        "enabled": row[1],
                        "count": row[2],
                    }
                    for row in keyword_rows.all()
                ],
                "approved_enabled_keywords": approved_count.scalar() or 0,
                "recent_attempt_status_counts": Counter([item.status for item in attempts.scalars().all()]),
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
    finally:
        await close_db()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
