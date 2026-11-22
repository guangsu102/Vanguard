import asyncio
from datetime import datetime
from sqlalchemy import update
from app.core import database as db
from app.core.account.models import TelegramAccount, AccountStatus

async def update_spam_status():
    await db.init_db()
    async with db.async_session_factory() as session:
        now = datetime.utcnow()
        summary = "I’m very sorry that you had to contact me. Unfortunately, some actions can trigger a harsh response from our anti-spam systems... While the account is limited, you will not be able to send messages..."
        await session.execute(
            update(TelegramAccount)
            .where(TelegramAccount.id.in_([33, 35]))
            .values(
                spam_check_status="restricted",
                spam_checked_at=now,
                spam_check_summary=summary,
                restriction_reason="spambot_restricted",
                restriction_detected_at=now
            )
        )
        await session.commit()
        print("Updated Accounts 33 & 35 spam_check_status to restricted.")

if __name__ == "__main__":
    asyncio.run(update_spam_status())
