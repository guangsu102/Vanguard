"""Run or preview the targeted stage-three Persona retention cleanup.

Dry-run example (recommended before an operational run):

    python scripts/cleanup_persona_retention.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core import database as db_module  # noqa: E402
from app.core import redis as redis_module  # noqa: E402
from app.modules.owned_group.messaging_retention import run_persona_retention  # noqa: E402


def _bounded_batch_size(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("batch size must be an integer") from exc
    if not 1 <= value <= 500:
        raise argparse.ArgumentTypeError("batch size must be between 1 and 500")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview or execute the narrow owned-group Persona retention cleanup."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Count eligible rows without writes.")
    mode.add_argument("--execute", action="store_true", help="Apply the retention cleanup.")
    parser.add_argument(
        "--batch-size",
        type=_bounded_batch_size,
        default=500,
        metavar="1..500",
    )
    return parser


async def _run(*, dry_run: bool, batch_size: int) -> dict[str, int | bool]:
    await db_module.init_db(create_tables=False)
    await redis_module.init_redis()
    try:
        async with db_module.get_db_session() as db:
            return await run_persona_retention(
                db,
                redis_client=redis_module.redis_client,
                dry_run=dry_run,
                batch_size=batch_size,
            )
    finally:
        try:
            await redis_module.close_redis()
        finally:
            await db_module.close_db()


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    result = asyncio.run(_run(dry_run=bool(args.dry_run), batch_size=args.batch_size))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if int(result.get("failed", 0)) else 0


if __name__ == "__main__":
    raise SystemExit(main())
