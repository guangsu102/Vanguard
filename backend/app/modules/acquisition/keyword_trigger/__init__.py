"""
Keyword Trigger Module Initialization

Exports keyword trigger functionality.
"""

from app.modules.acquisition.keyword_trigger.matcher import KeywordMatcher
from app.modules.acquisition.keyword_trigger.handler import TriggerHandler, TriggerResult
from app.modules.acquisition.keyword_trigger.actions import ActionExecutor, TriggerActionType

__all__ = [
    "KeywordMatcher",
    "TriggerHandler",
    "TriggerResult",
    "ActionExecutor",
    "TriggerActionType",
]
