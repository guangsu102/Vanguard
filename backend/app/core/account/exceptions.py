"""
Account Module Exceptions

Custom exceptions for account management operations.
"""


class AccountError(Exception):
    """Base exception for account-related errors."""
    pass


class AccountNotFoundError(AccountError):
    """Raised when an account is not found."""

    def __init__(self, identifier: str):
        self.identifier = identifier
        super().__init__(f"Account not found: {identifier}")


class AccountAlreadyExistsError(AccountError):
    """Raised when trying to create an account that already exists."""

    def __init__(self, phone: str):
        self.phone = phone
        super().__init__(f"Account already exists with phone: {phone}")


class AccountSessionError(AccountError):
    """Raised when there's an error with account session."""

    def __init__(self, account_id: int, message: str):
        self.account_id = account_id
        super().__init__(f"Session error for account {account_id}: {message}")


class AccountAuthenticationError(AccountError):
    """Raised when account authentication fails."""

    def __init__(self, account_id: int, message: str = "Authentication failed"):
        self.account_id = account_id
        super().__init__(f"Authentication error for account {account_id}: {message}")


class AccountBannedError(AccountError):
    """Raised when an account is banned."""

    def __init__(self, account_id: int):
        self.account_id = account_id
        super().__init__(f"Account {account_id} has been banned")


class AccountConnectionError(AccountError):
    """Raised when there's a connection error with Telegram."""

    def __init__(self, account_id: int, message: str):
        self.account_id = account_id
        super().__init__(f"Connection error for account {account_id}: {message}")


class ProxyError(Exception):
    """Base exception for proxy-related errors."""
    pass


class ProxyNotFoundError(ProxyError):
    """Raised when a proxy is not found."""

    def __init__(self, identifier: str):
        self.identifier = identifier
        super().__init__(f"Proxy not found: {identifier}")


class ProxyConnectionError(ProxyError):
    """Raised when there's an error connecting through proxy."""

    def __init__(self, proxy_info: str, message: str):
        self.proxy_info = proxy_info
        super().__init__(f"Proxy connection error ({proxy_info}): {message}")


class ProxyProviderError(ProxyError):
    """Raised when there's an error with the proxy provider API."""

    def __init__(self, provider: str, message: str):
        self.provider = provider
        super().__init__(f"Proxy provider error ({provider}): {message}")


class SessionNotFoundError(AccountError):
    """Raised when session file is not found."""

    def __init__(self, session_name: str):
        self.session_name = session_name
        super().__init__(f"Session not found: {session_name}")


class SessionExpiredError(AccountError):
    """Raised when session has expired."""

    def __init__(self, session_name: str):
        self.session_name = session_name
        super().__init__(f"Session expired: {session_name}")


class APIConfigError(Exception):
    """Raised when there's a configuration error."""
    pass


class InvalidAPIConfigError(APIConfigError):
    """Raised when API configuration is invalid."""

    def __init__(self, config_name: str, message: str):
        self.config_name = config_name
        super().__init__(f"Invalid API config '{config_name}': {message}")
