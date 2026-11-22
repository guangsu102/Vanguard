import asyncio
from app.core import database as db
from app.core.account.models import Proxy

async def check():
    await db.init_db()
    async with db.async_session_factory() as session:
        p = await session.get(Proxy, 28)
        if p:
            print(f"Proxy 28: host={p.host}:{p.port}, type={p.proxy_type}, is_active={p.is_active}, avg_latency={p.avg_latency}, last_checked={p.last_checked}")

if __name__ == "__main__":
    asyncio.run(check())
