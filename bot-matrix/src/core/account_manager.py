"""Telegram 账号管理器 - 支持用户账号矩阵"""
import asyncio
from typing import List, Optional, Dict, Any
from dataclasses import dataclass
from pathlib import Path
from itertools import cycle

from telethon import TelegramClient
from loguru import logger


@dataclass
class TelegramAccount:
    """Telegram 账号配置"""
    name: str
    enabled: bool
    session_name: str
    phone: str
    api_id: int
    api_hash: str
    is_bot: bool = False  # 是否是 Bot 账号


class AccountManager:
    """Telegram 账号管理器 - 管理多账号矩阵"""

    def __init__(self, session_dir: str = "./sessions"):
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(exist_ok=True)

        self._accounts: Dict[str, TelegramAccount] = {}
        self._clients: Dict[str, TelegramClient] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def add_account(self, name: str, account: TelegramAccount):
        """添加账号配置"""
        self._accounts[name] = account
        self._locks[name] = asyncio.Lock()
        logger.info(f"添加账号: {name} ({account.phone})")

    def get_account(self, name: str) -> Optional[TelegramAccount]:
        """获取账号配置"""
        return self._accounts.get(name)

    def get_enabled_accounts(self, module: str = None) -> List[TelegramAccount]:
        """获取已启用的账号列表"""
        accounts = []
        for name, acc in self._accounts.items():
            if acc.enabled:
                if module is None or name.startswith(module):
                    accounts.append(acc)
        return accounts

    async def create_client(self, name: str) -> Optional[TelegramClient]:
        """创建单个账号的客户端"""
        account = self._accounts.get(name)
        if not account:
            logger.error(f"账号不存在: {name}")
            return None

        if not account.enabled:
            logger.warning(f"账号未启用: {name}")
            return None

        # 检查是否已有客户端
        if name in self._clients and self._clients[name].is_connected():
            return self._clients[name]

        session_path = self.session_dir / account.session_name

        try:
            client = TelegramClient(
                session=str(session_path),
                api_id=account.api_id,
                api_hash=account.api_hash,
            )

            await client.start(phone=account.phone)

            self._clients[name] = client
            me = await client.get_me()
            logger.info(f"账号连接成功: {name} ({me.username or me.phone})")

            return client

        except Exception as e:
            logger.error(f"账号连接失败: {name} - {e}")
            return None

    async def get_client(self, name: str) -> Optional[TelegramClient]:
        """获取已连接的客户端（自动重连）"""
        if name in self._clients and self._clients[name].is_connected():
            return self._clients[name]

        return await self.create_client(name)

    async def connect_all(self) -> Dict[str, TelegramClient]:
        """连接所有启用的账号"""
        results = {}

        for name, account in self._accounts.items():
            if account.enabled:
                client = await self.create_client(name)
                if client:
                    results[name] = client

        logger.info(f"账号连接完成: {len(results)}/{len(self._accounts)} 个成功")
        return results

    async def disconnect(self, name: str):
        """断开单个账号"""
        if name in self._clients:
            await self._clients[name].disconnect()
            del self._clients[name]
            logger.info(f"账号已断开: {name}")

    async def disconnect_all(self):
        """断开所有账号"""
        for name in list(self._clients.keys()):
            await self.disconnect(name)
        logger.info("所有账号已断开")

    async def broadcast(self, module: str, func, *args, **kwargs):
        """向指定模块的所有账号广播消息"""
        results = {}

        for name, account in self._accounts.items():
            if account.enabled and name.startswith(module):
                async with self._locks[name]:
                    client = await self.get_client(name)
                    if client:
                        try:
                            result = await func(client, *args, **kwargs)
                            results[name] = {"success": True, "result": result}
                        except Exception as e:
                            results[name] = {"success": False, "error": str(e)}

        return results

    def get_account_info(self, name: str) -> Optional[Dict[str, Any]]:
        """获取账号信息"""
        account = self._accounts.get(name)
        client = self._clients.get(name)

        if not account:
            return None

        return {
            "name": account.name,
            "enabled": account.enabled,
            "phone": account.phone,
            "connected": client.is_connected() if client else False,
        }


class AccountPool:
    """账号池 - 支持账号轮询和负载均衡"""

    def __init__(self, account_manager: AccountManager):
        self.manager = account_manager
        self._pools: Dict[str, List[str]] = {}  # module -> [account_names]
        self._pool_iters: Dict[str, Any] = {}

    def register_pool(self, module: str, account_names: List[str]):
        """注册账号池"""
        self._pools[module] = account_names
        self._pool_iters[module] = cycle(account_names) if account_names else None
        logger.info(f"注册账号池: {module} -> {account_names}")

    def get_next_account(self, module: str) -> Optional[str]:
        """获取下一个可用账号（轮询）"""
        if module not in self._pools or not self._pools[module]:
            return None

        iterator = self._pool_iters.get(module)
        if iterator is None:
            pool = self._pools[module]
            if not pool:
                return None
            iterator = cycle(pool)
            self._pool_iters[module] = iterator

        return next(iterator, None)

    async def execute_with_pool(
        self,
        module: str,
        func,
        *args,
        **kwargs
    ) -> Optional[Any]:
        """使用账号池执行操作"""
        account_name = self.get_next_account(module)
        if not account_name:
            return None

        client = await self.manager.get_client(account_name)
        if not client:
            return None

        return await func(client, *args, **kwargs)
