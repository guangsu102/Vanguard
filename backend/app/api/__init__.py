"""
API Package Initialization
"""

from app.api.account_personas import router as account_personas
from app.api.account_spam import router as account_spam
from app.api.account_profile_updates import router as account_profile_updates
from app.api.accounts import router as accounts
from app.api.acquisition import router as acquisition
from app.api.ad_only_recommendations import router as ad_only_recommendations
from app.api.auth import router as auth
from app.api.automation import router as automation
from app.api.broadcasts import router as broadcasts
from app.api.campaigns import router as campaigns
from app.api.group_governance import router as group_governance
from app.api.group_search_keywords import router as group_search_keywords
from app.api.groups import router as groups
from app.api.guardian_bots import router as guardian_bots
from app.api.keywords import router as keywords
from app.api.managed_bot_provisions import router as managed_bot_provisions
from app.api.managed_groups import router as managed_groups
from app.api.moderation import router as moderation
from app.api.moderation_sensitive_keywords import router as moderation_sensitive_keywords
from app.api.owned_group_audit import router as owned_group_audit
from app.api.owned_group_bots import router as owned_group_bots
from app.api.owned_group_controls import router as owned_group_controls
from app.api.owned_group_governance import router as owned_group_governance
from app.api.owned_group_invites import router as owned_group_invites
from app.api.owned_group_messages import router as owned_group_messages
from app.api.owned_group_operations import router as owned_group_operations
from app.api.owned_groups import router as owned_groups
from app.api.private_chats import router as private_chats
from app.api.proxies import router as proxies
from app.api.punishments import router as punishments
from app.api.qq import router as qq
from app.api.resource_search import router as resource_search
from app.api.rules import router as rules
from app.api.stats import router as stats
from app.api.sub2api_alerts import router as sub2api_alerts
from app.api.users import router as users
from app.api.verification import router as verification
from app.api.websocket import router as websocket
from app.api.workers import router as workers
from app.api.xboard import router as xboard

__all__ = [
    "auth",
    "accounts",
    "account_spam",
    "account_profile_updates",
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
    "ad_only_recommendations",
    "group_governance",
    "group_search_keywords",
    "guardian_bots",
    "managed_bot_provisions",
    "managed_groups",
    "moderation_sensitive_keywords",
    "workers",
    "qq",
    "private_chats",
    "sub2api_alerts",
    "resource_search",
    "owned_groups",
    "owned_group_controls",
    "owned_group_audit",
    "owned_group_bots",
    "owned_group_invites",
    "owned_group_governance",
    "owned_group_messages",
    "account_personas",
    "owned_group_operations",
]
