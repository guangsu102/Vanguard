from __future__ import annotations

import asyncio

import bcrypt
from sqlalchemy import text

from app.core.database import close_db, get_db_session, init_db


USERNAME = "admin"
PASSWORD = "Wycqq123456!@#"


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
    await init_db(create_tables=False)
    password_hash = bcrypt.hashpw(PASSWORD.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    async with get_db_session() as session:
        await session.execute(text(CREATE_ADMIN_TABLE_SQL))
        existing = await session.execute(
            text("SELECT id FROM admin_user WHERE username = :username"),
            {"username": USERNAME},
        )
        row = existing.first()
        if row:
            await session.execute(
                text(
                    """
                    UPDATE admin_user
                    SET password = :password, role = 'admin', is_active = TRUE, updated_at = NOW()
                    WHERE username = :username
                    """
                ),
                {"username": USERNAME, "password": password_hash},
            )
        else:
            await session.execute(
                text(
                    """
                    INSERT INTO admin_user (username, password, role, email, is_active)
                    VALUES (:username, :password, 'admin', 'admin@vanguard.local', TRUE)
                    """
                ),
                {"username": USERNAME, "password": password_hash},
            )
    await close_db()
    print("admin user ready")


if __name__ == "__main__":
    asyncio.run(main())
