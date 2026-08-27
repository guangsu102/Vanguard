import logging
from logging.handlers import TimedRotatingFileHandler

from app.core.config import settings
from app.core.logging import setup_logging


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
