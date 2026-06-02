#!/usr/bin/env python3
"""Update admin user password"""
import asyncio
import bcrypt
from sqlalchemy import text
from app.core.database import get_db_session


async def update_password():
    """Update admin password to admin123"""
    password = "admin123"
    salt = bcrypt.gensalt()
    password_hash = bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

    async with get_db_session() as db:
        await db.execute(
            text("UPDATE admin_user SET password = :pwd WHERE username = :user"),
            {"pwd": password_hash, "user": "admin"}
        )
        await db.commit()

    print(f"Password updated successfully")
    print(f"Username: admin")
    print(f"Password: admin123")


if __name__ == "__main__":
    asyncio.run(update_password())
