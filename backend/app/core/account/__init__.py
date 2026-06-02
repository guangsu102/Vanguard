"""
Account Module Initialization.

Exports are resolved lazily so configuration/state code can import account
models without loading Telethon or proxy clients.
"""

__all__ = [
    "AccountStatus",
    "ProxyType",
    "SessionType",
    "AccountOperationConfig",
    "TelegramAccount",
    "TelegramAPIConfig",
    "AccountManager",
    "AccountPool",
    "TelegramAccountWrapper",
    "get_account_pool",
    "init_account_pool",
    "close_account_pool",
    "DecodoClient",
    "EvomiClient",
    "ProxyInfo",
    "DecodoSessionType",
    "get_decodo_client",
    "init_decodo_client",
    "close_decodo_client",
    "get_evomi_client",
    "init_evomi_client",
    "close_evomi_client",
    "AccountError",
    "AccountNotFoundError",
    "AccountAlreadyExistsError",
    "AccountSessionError",
    "AccountAuthenticationError",
    "AccountBannedError",
    "AccountConnectionError",
    "ProxyError",
    "ProxyNotFoundError",
    "ProxyConnectionError",
    "ProxyProviderError",
    "SessionNotFoundError",
    "SessionExpiredError",
    "APIConfigError",
    "InvalidAPIConfigError",
]


def __getattr__(name: str):
    if name in {
        "AccountStatus",
        "ProxyType",
        "SessionType",
        "AccountOperationConfig",
        "TelegramAccount",
        "TelegramAPIConfig",
    }:
        from app.core.account import models

        return getattr(models, name)

    if name == "AccountManager":
        from app.core.account.manager import AccountManager

        return AccountManager

    if name in {"AccountPool", "TelegramAccountWrapper", "get_account_pool", "init_account_pool", "close_account_pool"}:
        from app.core.account import pool

        return getattr(pool, name)

    if name in {"DecodoClient", "get_decodo_client", "init_decodo_client", "close_decodo_client"}:
        from app.core.account import decodo

        return getattr(decodo, name)

    if name in {"EvomiClient", "ProxyInfo", "get_evomi_client", "init_evomi_client", "close_evomi_client"}:
        from app.core.account import evomi

        return getattr(evomi, name)

    if name == "DecodoSessionType":
        from app.core.account.decodo import SessionType

        return SessionType

    if name in {
        "AccountError",
        "AccountNotFoundError",
        "AccountAlreadyExistsError",
        "AccountSessionError",
        "AccountAuthenticationError",
        "AccountBannedError",
        "AccountConnectionError",
        "ProxyError",
        "ProxyNotFoundError",
        "ProxyConnectionError",
        "ProxyProviderError",
        "SessionNotFoundError",
        "SessionExpiredError",
        "APIConfigError",
        "InvalidAPIConfigError",
    }:
        from app.core.account import exceptions

        return getattr(exceptions, name)

    raise AttributeError(name)
