"""Restore rollback-paused switches only when no intervening edit occurred."""
from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESTORE = r'''
import asyncio,json,os
import asyncpg
async def main():
 c=await asyncpg.connect(os.environ['DATABASE_URL'].replace('postgresql+asyncpg:', 'postgresql:'),timeout=15)
 restored=[]
 try:
  async with c.transaction():
   raw=await c.fetchval('select value from system_setting where key=$1 for update','growth.writable_v2.rollback_pause')
   if not raw: raise RuntimeError('rollback switch backup missing')
   previous=json.loads(raw)
   for key,original in sorted(previous.items()):
    current=await c.fetchval('select value from system_setting where key=$1 for update',key)
    expected=json.loads(original or '{}')
    expected['enabled']=False
    if key.endswith('group_qualification'):
     expected.update(execute_reviews=False,execute_verification=False,execute_exits=False)
    if json.loads(current or '{}') != expected:
     raise RuntimeError('switch changed since rollback: '+key)
    if original is None:
     await c.execute('delete from system_setting where key=$1',key)
    else:
     await c.execute('update system_setting set value=$2,updated_at=now() where key=$1',key,original)
    restored.append(key)
 finally:
  await c.close()
 print(json.dumps({'restored':restored}))
asyncio.run(main())
'''


def main() -> None:
    marker = ROOT / 'p0-resumed.json'
    if marker.exists():
        raise RuntimeError('already resumed; inspect marker and switch state')
    subprocess.run(['docker', 'exec', '-i', 'vanguard-backend', 'python', '-'],
                   input=RESTORE, text=True, check=True, timeout=60)
    marker.write_text(json.dumps({'started_at': datetime.now(UTC).isoformat(),
                                 'source': 'original rollback switch backup'}))


if __name__ == '__main__':
    main()
