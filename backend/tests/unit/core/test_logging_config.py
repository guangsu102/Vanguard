import logging
from logging.handlers import TimedRotatingFileHandler

from app.core.config import settings
from app.core.logging import TelegramAccountLogContextFilter, setup_logging


def test_telethon_account_logs_include_account_id():
    record = logging.LogRecord(
        name="vanguard.telethon.account.7.telethon.network.mtprotosender",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="Server closed the connection: %s",
        args=("EOF",),
        exc_info=None,
    )

    assert TelegramAccountLogContextFilter().filter(record) is True
    assert record.getMessage() == "[telegram account_id=7] Server closed the connection: EOF"


def test_setup_logging_configures_daily_retention(tmp_path):
    original = {
        "LOG_FILE": settings.LOG_FILE,
        "LOG_LEVEL": settings.LOG_LEVEL,
        "LOG_STDOUT_LEVEL": settings.LOG_STDOUT_LEVEL,
        "LOG_RETENTION_DAYS": settings.LOG_RETENTION_DAYS,
    }
    try:
        settings.LOG_FILE = str(tmp_path / "growth-worker.log")
        settings.LOG_LEVEL = "INFO"
        settings.LOG_STDOUT_LEVEL = "WARNING"
        settings.LOG_RETENTION_DAYS = 15

        setup_logging()

        root_handlers = logging.getLogger().handlers
        file_handler = next(
            handler for handler in root_handlers if isinstance(handler, TimedRotatingFileHandler)
        )
        stream_handler = next(
            handler
            for handler in root_handlers
            if isinstance(handler, logging.StreamHandler)
            and not isinstance(handler, TimedRotatingFileHandler)
        )

        assert file_handler.backupCount == 15
        assert file_handler.when == "MIDNIGHT"
        assert file_handler.utc is True
        assert file_handler.level == logging.INFO
        assert stream_handler.level == logging.WARNING
    finally:
        settings.LOG_FILE = original["LOG_FILE"]
        settings.LOG_LEVEL = original["LOG_LEVEL"]
        settings.LOG_STDOUT_LEVEL = original["LOG_STDOUT_LEVEL"]
        settings.LOG_RETENTION_DAYS = original["LOG_RETENTION_DAYS"]
        setup_logging()
