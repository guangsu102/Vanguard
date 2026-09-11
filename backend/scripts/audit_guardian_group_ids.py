"""Audit Guardian group identifier history without writing to the database."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.database import close_db, get_db_session, init_db  # noqa: E402
from app.modules.guardian.id_audit import audit_guardian_group_ids  # noqa: E402


async def _run() -> int:
    await init_db(create_tables=False)
    try:
        async with get_db_session() as db:
            bind = db.get_bind()
            if bind.dialect.name == "postgresql":
                await db.execute(text("SET TRANSACTION READ ONLY"))
            report = await audit_guardian_group_ids(db)
            print(report.to_json())
            return report.exit_code
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "guardian_group_id_audit_failed",
                    "error_type": type(exc).__name__,
                },
                sort_keys=True,
            )
        )
        return 2
    finally:
        await close_db()


def _build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description=(
            "Read-only audit for Guardian core-group and Telegram chat ID mappings. "
            "The configured DATABASE_URL is used only after argument parsing succeeds."
        )
    )


def main(argv: list[str] | None = None) -> int:
    _build_parser().parse_args(argv)
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
