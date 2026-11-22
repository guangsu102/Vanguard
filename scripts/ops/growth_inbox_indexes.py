"""Run inside a Vanguard application container; only adds online inbox indexes."""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

import asyncpg


async def main():
    path = Path('/tmp/078_growth_inbox_indexes.sql')
    sql = path.read_text()
    checksum = hashlib.sha256(sql.encode()).hexdigest()
    connection = await asyncpg.connect(os.environ['DATABASE_URL'].replace('postgresql+asyncpg:', 'postgresql:'), timeout=15)
    try:
        await connection.execute("SET statement_timeout = '15min'")
        await connection.execute('SELECT pg_advisory_lock(781246903)')
        previous = await connection.fetchval('SELECT checksum FROM schema_migration_history WHERE filename=$1', path.name)
        if previous is not None and previous != checksum:
            raise RuntimeError('migration checksum mismatch')
        for statement in sql.split(';'):
            if 'CREATE INDEX IF NOT EXISTS' not in statement:
                continue
            await connection.execute(statement.replace('CREATE INDEX IF NOT EXISTS', 'CREATE INDEX CONCURRENTLY IF NOT EXISTS'))
        valid = await connection.fetchval("SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
            "WHERE c.relname IN ('ix_telegram_event_inbox_live_peer','ix_telegram_event_inbox_live_account') AND i.indisvalid")
        if valid != 2:
            raise RuntimeError('both inbox indexes must be valid before recording migration')
        await connection.execute('INSERT INTO schema_migration_history(filename,checksum) VALUES($1,$2) ON CONFLICT(filename) DO NOTHING',path.name,checksum)
        print('078 applied: two concurrent inbox indexes are valid', flush=True)
    finally:
        await connection.close()


if __name__ == '__main__':
    asyncio.run(main())
