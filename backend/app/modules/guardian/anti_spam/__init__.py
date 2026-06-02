"""
Anti-Spam Module

Spam detection and competitor blocking.
"""

from app.modules.guardian.anti_spam.spam_detector import SpamDetector
from app.modules.guardian.anti_spam.competitor_block import CompetitorBlocker

__all__ = [
    "SpamDetector",
    "CompetitorBlocker",
]
