"""Add the bounded retention index online, only inside Vanguard's database."""
import subprocess

CODE = '''import asyncio,os,json,asyncpg
async def main():
 c=await asyncpg.connect(os.environ['DATABASE_URL'].replace('postgresql+asyncpg:','postgresql:'))
 try:
  assert await c.fetchval('SELECT current_database()') == 'vanguard'
  await c.execute("SET lock_timeout='2s'")
  await c.execute("SET statement_timeout='300s'")
  name='ix_send_result_completed_fact_retention'
  valid=await c.fetchval("SELECT indisvalid FROM pg_index WHERE indexrelid=to_regclass($1)",name)
  if valid is False:
   await c.execute('DROP INDEX CONCURRENTLY '+name)
  await c.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS "+name+" ON telegram_event_inbox(account_id,completed_at,id) WHERE state='completed' AND kind='listener_fact' AND external_attempted IS FALSE")
  print(json.dumps({'retention_index_valid':await c.fetchval("SELECT indisvalid FROM pg_index WHERE indexrelid=to_regclass($1)",name)}))
 finally:await c.close()
asyncio.run(main())'''

if __name__ == '__main__':
    result = subprocess.run(['docker', 'exec', '-i', 'vanguard-backend', 'python', '-'], input=CODE,
                            text=True, capture_output=True)
    if result.returncode:
        print(result.stderr[-1000:])
        raise SystemExit(result.returncode)
    print(result.stdout)
