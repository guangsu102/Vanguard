"""
Vanguard Logging Configuration

Structured logging with structlog.
"""

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

import structlog

from app.core.config import settings


def setup_logging() -> None:
    """Configure structured logging."""

    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    stdout_level = getattr(
        logging,
        settings.LOG_STDOUT_LEVEL.upper(),
        log_level,
    )

    handlers: list[logging.Handler] = []
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(stdout_level)
    handlers.append(stdout_handler)

    if settings.LOG_FILE:
        log_path = Path(settings.LOG_FILE)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = TimedRotatingFileHandler(
            log_path,
            when="midnight",
            interval=1,
            backupCount=settings.LOG_RETENTION_DAYS,
            encoding="utf-8",
            delay=True,
            utc=True,
        )
        file_handler.setLevel(log_level)
        handlers.append(file_handler)

    root_level = min(handler.level for handler in handlers)

    logging.basicConfig(
        format="%(message)s",
        handlers=handlers,
        level=root_level,
        force=True,
    )

    # Configure structlog
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer() if settings.is_production
                else structlog.dev.ConsoleRenderer(colors=True),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Set levels for third-party loggers
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str | None = None, **kwargs: Any) -> structlog.BoundLogger:
    """Get a logger instance."""
    logger = structlog.get_logger()
    if name:
        logger = logger.bind(module=name)
    if kwargs:
        logger = logger.bind(**kwargs)
    return logger


# Global logger instance
logger = get_logger("vanguard")
