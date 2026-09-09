"""Create or rotate the first Vanguard administrator from environment values."""

from __future__ import annotations

import asyncio
import os

import bcrypt
from sqlalchemy import text

from app.core.database import close_db, get_db_session, init_db


CREATE_ADMIN_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS admin_user (
    id SERIAL PRIMARY KEY,
    username VARCHAR(50) NOT NULL UNIQUE,
    password VARCHAR(255) NOT NULL,
    role VARCHAR(20) NOT NULL DEFAULT 'operator',
    email VARCHAR(100),
    avatar VARCHAR(255),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    last_login_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
)
"""


async def main() -> None:
    username = (os.getenv("VANGUARD_ADMIN_USERNAME") or "admin").strip()
    password = os.getenv("VANGUARD_ADMIN_PASSWORD") or ""
    if not username or len(username) > 50:
        raise SystemExit("VANGUARD_ADMIN_USERNAME must be 1-50 characters")
    if len(password) < 16:
        raise SystemExit("VANGUARD_ADMIN_PASSWORD must contain at least 16 characters")

    await init_db(create_tables=False)
    try:
        password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        async with get_db_session() as session:
            await session.execute(text(CREATE_ADMIN_TABLE_SQL))
            await session.execute(
                text(
                    """
                    INSERT INTO admin_user (username, password, role, email, is_active)
                    VALUES (:username, :password, 'admin', :email, TRUE)
                    ON CONFLICT (username) DO UPDATE
                    SET password = EXCLUDED.password,
                        role = 'admin',
                        is_active = TRUE,
                        updated_at = NOW()
                    """
                ),
                {
                    "username": username,
                    "password": password_hash,
                    "email": f"{username}@vanguard.local",
                },
            )
    finally:
        await close_db()
    print(f"administrator ready: {username}")


if __name__ == "__main__":
    asyncio.run(main())
