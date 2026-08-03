"""
Auto Reply Module Initialization

Exports auto-reply and speaking functionality.
"""

from app.modules.acquisition.auto_reply.speaker import Speaker
from app.modules.acquisition.auto_reply.reply_engine import ReplyEngine
from app.modules.acquisition.auto_reply.semantic_reply import SemanticGroupReplyEngine
from app.modules.acquisition.auto_reply.templates import TemplateEngine, MessageTemplateStore
from app.modules.acquisition.auto_reply.scheduler import SpeakScheduler, SpeakSchedule

__all__ = [
    "Speaker",
    "ReplyEngine",
    "SemanticGroupReplyEngine",
    "TemplateEngine",
    "MessageTemplateStore",
    "SpeakScheduler",
    "SpeakSchedule",
]
