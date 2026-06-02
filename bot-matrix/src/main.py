"""XBoard Bot Matrix - 主程序入口

支持 Telegram 用户账号矩阵（多账号运营）
"""
import asyncio
import signal
import sys
import os
from pathlib import Path

import yaml
from loguru import logger

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.bots.lead_gen import LeadGenBot
from src.bots.service import ServiceBot
from src.bots.group_ops import GroupOpsBot
from src.core.database import Database
from src.core.cache import RedisClient
from src.core.api import XBoardAPIClient
from src.core.account_manager import AccountManager, TelegramAccount
from src.utils.poster import PosterGenerator


class BotMatrix:
    """Bot 矩阵管理器"""

    def __init__(self, config_path: str = "config/config.yaml"):
        self.config = self._load_config(config_path)
        self.db: Database = None
        self.redis: RedisClient = None
        self.api: XBoardAPIClient = None
        self.poster: PosterGenerator = None
        self.account_manager: AccountManager = None

        self.lead_gen_bot: LeadGenBot = None
        self.service_bot: ServiceBot = None
        self.group_ops_bot: GroupOpsBot = None

        self._shutdown_event = asyncio.Event()

    def _load_config(self, config_path: str) -> dict:
        """加载配置文件"""
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_path}")

        with open(path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        # 替换环境变量
        config = self._replace_env_vars(config)

        logger.info("配置文件加载成功")
        return config

    def _replace_env_vars(self, config: dict) -> dict:
        """递归替换环境变量占位符"""
        result = {}
        for key, value in config.items():
            if isinstance(value, dict):
                result[key] = self._replace_env_vars(value)
            elif isinstance(value, str) and value.startswith("${") and value.endswith("}"):
                env_var = value[2:-1]
                parts = env_var.split(":")
                env_value = os.getenv(parts[0], parts[1] if len(parts) > 1 else "")
                result[key] = env_value
            elif isinstance(value, list):
                result[key] = [
                    self._replace_env_vars(item) if isinstance(item, dict) else item
                    for item in value
                ]
            else:
                result[key] = value
        return result

    def _setup_logging(self):
        """配置日志"""
        log_level = self.config["app"].get("log_level", "INFO")

        logger.remove()
        logger.add(
            sys.stderr,
            format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
            level=log_level
        )

        # 写入日志文件
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        logger.add(
            log_dir / "bot_matrix_{time:YYYY-MM-DD}.log",
            rotation="00:00",
            retention="30 days",
            level=log_level
        )

    async def initialize(self):
        """初始化所有组件"""
        logger.info("开始初始化 Bot 矩阵...")

        # 初始化日志
        self._setup_logging()

        # 初始化账号管理器
        self.account_manager = AccountManager(session_dir="./sessions")
        self._register_accounts()

        # 初始化数据库
        db_config = self.config["database"]
        db_dsn = f"postgresql+asyncpg://{db_config['user']}:{db_config['password']}@{db_config['host']}:{db_config['port']}/{db_config['database']}"
        self.db = Database(db_dsn)
        await self.db.init()
        logger.info("数据库初始化完成")

        # 初始化 Redis
        redis_config = self.config["redis"]
        self.redis = RedisClient(
            host=redis_config["host"],
            port=redis_config["port"],
            password=redis_config.get("password") or None,
            db=redis_config.get("db", 0)
        )
        await self.redis.connect()
        logger.info("Redis 初始化完成")

        # 初始化 XBoard API 客户端
        api_config = self.config["xboard_api"]
        self.api = XBoardAPIClient(
            base_url=api_config["base_url"],
            api_key=api_config["api_key"],
            timeout=api_config.get("timeout", 30),
            retry_times=api_config.get("retry_times", 3),
            retry_delay=api_config.get("retry_delay", 5)
        )
        logger.info("XBoard API 客户端初始化完成")

        # 初始化海报生成器
        poster_config = self.config["service"]["poster"]
        self.poster = PosterGenerator(
            output_dir=poster_config.get("output_dir", "./assets/posters"),
            width=poster_config.get("width", 800),
            height=poster_config.get("height", 1200),
            qr_size=poster_config.get("qr_size", 200)
        )
        logger.info("海报生成器初始化完成")

        # 连接所有 Telegram 账号
        await self.account_manager.connect_all()

        # 初始化 Bot 实例
        self._init_bots()

        logger.info("Bot 矩阵初始化完成")

    def _register_accounts(self):
        """注册所有 Telegram 账号"""
        tg_config = self.config.get("telegram", {})

        # 注册引流账号
        for idx, acc_config in enumerate(tg_config.get("lead_gen", []), 1):
            account = TelegramAccount(
                name=acc_config["name"],
                enabled=acc_config.get("enabled", True),
                session_name=acc_config["session_name"],
                phone=acc_config["phone"],
                api_id=int(acc_config["api_id"]),
                api_hash=acc_config["api_hash"]
            )
            self.account_manager.add_account(acc_config["session_name"], account)

        # 注册运营账号
        for idx, acc_config in enumerate(tg_config.get("service", []), 1):
            account = TelegramAccount(
                name=acc_config["name"],
                enabled=acc_config.get("enabled", True),
                session_name=acc_config["session_name"],
                phone=acc_config["phone"],
                api_id=int(acc_config["api_id"]),
                api_hash=acc_config["api_hash"]
            )
            self.account_manager.add_account(acc_config["session_name"], account)

        # 注册管理账号
        for idx, acc_config in enumerate(tg_config.get("group_ops", []), 1):
            account = TelegramAccount(
                name=acc_config["name"],
                enabled=acc_config.get("enabled", True),
                session_name=acc_config["session_name"],
                phone=acc_config["phone"],
                api_id=int(acc_config["api_id"]),
                api_hash=acc_config["api_hash"]
            )
            self.account_manager.add_account(acc_config["session_name"], account)

        logger.info(f"已注册 {len(self.account_manager._accounts)} 个 Telegram 账号")

    def _init_bots(self):
        """初始化所有 Bot"""
        # 引流空投 Bot
        self.lead_gen_bot = LeadGenBot(
            account_manager=self.account_manager,
            db=self.db,
            redis=self.redis,
            api=self.api,
            config=self.config
        )
        logger.info("引流空投 Bot 初始化完成")

        # 核心运营 Bot
        self.service_bot = ServiceBot(
            account_manager=self.account_manager,
            db=self.db,
            redis=self.redis,
            api=self.api,
            poster=self.poster,
            config=self.config
        )
        logger.info("核心运营 Bot 初始化完成")

        # 社群风纪 Bot
        self.group_ops_bot = GroupOpsBot(
            account_manager=self.account_manager,
            db=self.db,
            redis=self.redis,
            api=self.api,
            config=self.config
        )
        logger.info("社群风纪 Bot 初始化完成")

    async def start(self):
        """启动所有 Bot"""
        logger.info("启动 Bot 矩阵...")

        # 启动各模块
        await self.lead_gen_bot.start()
        await self.service_bot.start()
        await self.group_ops_bot.start()

        # 启动定时任务
        task = asyncio.create_task(self._run_scheduled_tasks())

        try:
            # 保持运行
            await self._shutdown_event.wait()
        except asyncio.CancelledError:
            logger.info("收到取消信号，正在停止...")
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run_scheduled_tasks(self):
        """运行定时任务"""
        while not self._shutdown_event.is_set():
            try:
                # 弃单挽回检查（每分钟执行）
                await self.service_bot.process_abandoned_orders()

                # 节点状态播报（每天 20:30）
                await self._check_node_report()

            except Exception as e:
                logger.error(f"定时任务执行失败: {e}")

            # 等待 1 分钟
            await asyncio.sleep(60)

    async def _check_node_report(self):
        """检查是否需要发送节点状态播报"""
        from datetime import datetime

        report_config = self.config["group_ops"]["node_report"]
        if not report_config.get("enabled"):
            return

        now = datetime.now()
        schedule_time = report_config.get("schedule", "20:30")

        if now.hour == 20 and 25 <= now.minute <= 35:
            group_id = self.config["monitoring"].get("node_report_chat_id")
            if group_id:
                await self.group_ops_bot.send_node_report(group_id)

    async def stop(self):
        """停止所有 Bot"""
        logger.info("正在停止 Bot 矩阵...")

        self._shutdown_event.set()

        if self.lead_gen_bot:
            await self.lead_gen_bot.stop()
        if self.service_bot:
            await self.service_bot.stop()
        if self.group_ops_bot:
            await self.group_ops_bot.stop()

        if self.account_manager:
            await self.account_manager.disconnect_all()

        if self.redis:
            await self.redis.close()

        if self.db:
            await self.db.close()

        logger.info("Bot 矩阵已停止")


async def main():
    """主函数"""
    # 设置工作目录
    os.chdir(Path(__file__).parent.parent)

    matrix = BotMatrix()

    # 设置信号处理
    loop = asyncio.get_event_loop()

    def signal_handler():
        logger.info("收到 SIGINT/SIGTERM 信号")
        asyncio.create_task(matrix.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    try:
        await matrix.initialize()
        await matrix.start()
    except Exception as e:
        logger.error(f"Bot 矩阵运行出错: {e}")
        raise
    finally:
        await matrix.stop()


if __name__ == "__main__":
    asyncio.run(main())
