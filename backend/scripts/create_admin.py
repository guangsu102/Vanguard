#!/usr/bin/env python3
"""
Create initial admin user
"""
import asyncio
import sys
from passlib.context import CryptContext

sys.path.insert(0, '/app')

from app.core.database import get_db_connection

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


async def create_admin_user():
    """Create default admin user"""
    db = await get_db_connection()

    try:
        async with db.cursor() as cursor:
            # Check if admin user already exists
            await cursor.execute("SELECT id FROM admin_user WHERE username = 'admin'")
            existing = await cursor.fetchone()

            if existing:
                print("Admin user already exists")
                return

            # Create admin user
            # Password: admin123
            password_hash = pwd_context.hash("admin123")

            await cursor.execute(
                """
                INSERT INTO admin_user (username, password, role, email)
                VALUES (%s, %s, %s, %s)
                """,
                ("admin", password_hash, "admin", "admin@vanguard.local")
            )
            await db.commit()

            print("Admin user created successfully")
            print("Username: admin")
            print("Password: admin123")

    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(create_admin_user())
