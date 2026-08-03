"""QQ official group governance module."""

from app.modules.qq.models import (
    QQBotConnection,
    QQGroupCommand,
    QQGroupEvent,
    QQGroupMessage,
    QQManagedGroup,
)

__all__ = [
    "QQBotConnection",
    "QQGroupCommand",
    "QQGroupEvent",
    "QQGroupMessage",
    "QQManagedGroup",
]
