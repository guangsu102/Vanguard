"""
Core Modules Package Initialization
"""

__all__ = [
    "AccountManager",
    "AccountPool",
    "GroupManager",
    "KeywordEngine",
    "MessageRouter",
    "UserFSM",
    "CampaignEngine",
]


def __getattr__(name: str):
    if name in {"AccountManager", "AccountPool"}:
        from app.core.account import AccountManager, AccountPool

        return {"AccountManager": AccountManager, "AccountPool": AccountPool}[name]
    if name == "GroupManager":
        from app.core.group import GroupManager

        return GroupManager
    if name == "KeywordEngine":
        from app.core.keyword import KeywordEngine

        return KeywordEngine
    if name == "MessageRouter":
        from app.core.message import MessageRouter

        return MessageRouter
    if name == "UserFSM":
        from app.core.user import UserFSM

        return UserFSM
    if name == "CampaignEngine":
        from app.core.campaign import CampaignEngine

        return CampaignEngine
    raise AttributeError(name)
