"""
API Package Initialization
"""

from app.api.auth import router as auth
from app.api.accounts import router as accounts
from app.api.proxies import router as proxies
from app.api.groups import router as groups
from app.api.keywords import router as keywords
from app.api.users import router as users
from app.api.campaigns import router as campaigns
from app.api.rules import router as rules
from app.api.stats import router as stats
from app.api.websocket import router as websocket
from app.api.moderation import router as moderation
from app.api.verification import router as verification
from app.api.punishments import router as punishments
from app.api.acquisition import router as acquisition
from app.api.broadcasts import router as broadcasts
from app.api.xboard import router as xboard
from app.api.automation import router as automation
from app.api.group_governance import router as group_governance
from app.api.group_search_keywords import router as group_search_keywords
from app.api.guardian_bots import router as guardian_bots
from app.api.managed_groups import router as managed_groups
from app.api.moderation_sensitive_keywords import router as moderation_sensitive_keywords
from app.api.workers import router as workers
from app.api.qq import router as qq
from app.api.sub2api_alerts import router as sub2api_alerts

__all__ = [
    "auth",
    "accounts",
    "proxies",
    "groups",
    "keywords",
    "users",
    "campaigns",
    "rules",
    "stats",
    "websocket",
    "moderation",
    "verification",
    "punishments",
    "acquisition",
    "broadcasts",
    "xboard",
    "automation",
    "group_governance",
    "group_search_keywords",
    "guardian_bots",
    "managed_groups",
    "moderation_sensitive_keywords",
    "workers",
    "qq",
    "sub2api_alerts",
]
