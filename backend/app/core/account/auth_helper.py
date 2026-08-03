"""
Telegram Account Authentication Helper

Provides utilities for Telegram account login flow:
- Send verification code
- Verify code and complete login
- Handle 2FA authentication
- Session management
"""

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime, timedelta

import structlog
from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
    FloodWaitError,
)
from telethon.sessions import StringSession

from app.core.config import get_settings
from app.core.network.fingerprint import FingerprintManager

logger = structlog.get_logger()


@dataclass
class LoginSession:
    """Temporary login session data."""
    phone: str
    api_id: str
    api_hash: str
    phone_code_hash: str
    client: TelegramClient
    created_at: datetime
    expires_at: datetime
    requires_2fa: bool = False
    device_profile: Optional[Dict[str, str]] = None

    def is_expired(self) -> bool:
        """Check if session is expired."""
        return datetime.utcnow() > self.expires_at


class TelegramAuthHelper:
    """
    Helper class for Telegram account authentication.

    Manages the multi-step login process:
    1. Send code request
    2. Verify code
    3. Handle 2FA if needed
    4. Save session
    """

    def __init__(self):
        """Initialize auth helper."""
        self.settings = get_settings()
        self.logger = logger.bind(module="telegram_auth")
        self._login_sessions: Dict[str, LoginSession] = {}
        self._session_dir = Path(self.settings.TELEGRAM_SESSION_DIR)
        self._session_dir.mkdir(parents=True, exist_ok=True)

    def _get_session_path(self, session_name: str) -> Path:
        """Get session file path."""
        return self._session_dir / f"{session_name}.session"

    @staticmethod
    def _export_session_string(client: TelegramClient) -> str:
        """Export a Telethon client session as a portable string session."""
        session_string = StringSession.save(client.session)
        if not session_string:
            raise Exception("Failed to export Telegram session string")
        return session_string

    async def _get_required_proxy(self, country_code: str, account_key: str) -> tuple:
        """Acquire a required provider proxy for Telegram authentication."""
        provider = getattr(self.settings, "PROXY_PROVIDER", "evomi").lower()
        if provider == "decodo":
            from app.core.account.decodo import get_decodo_client

            proxy = (await get_decodo_client().get_proxy_for_account(country_code))[0]
        else:
            from app.core.account.evomi import get_evomi_client

            proxy = (await get_evomi_client().get_proxy_for_account(
                country_code,
                account_key=account_key,
            ))[0]

        return (
            proxy.protocol,
            proxy.host,
            proxy.port,
            True,
            proxy.username,
            proxy.password,
        )

    async def send_code(
        self,
        phone: str,
        api_id: str,
        api_hash: str,
        country_code: str = "US",
        account_key: Optional[str] = None,
        proxy: Optional[Dict[str, Any]] = None,
        proxy_required: Optional[bool] = None,
        device_profile: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Send verification code to phone number.

        Args:
            phone: Phone number with country code (e.g., +1234567890)
            api_id: Telegram API ID
            api_hash: Telegram API Hash
            country_code: ISO 3166-1 alpha-2 country code for proxy targeting
            account_key: Stable account identifier for sticky proxy sessions
            proxy: Optional proxy configuration

        Returns:
            Dict with session_id and phone_code_hash

        Raises:
            FloodWaitError: If rate limited
            Exception: Other Telegram errors
        """
        self.logger.info("sending_verification_code", phone=phone)

        should_require_proxy = (
            getattr(self.settings, "PROMOTER_PROXY_REQUIRED", True)
            if proxy_required is None
            else proxy_required
        )
        if proxy is None and should_require_proxy:
            proxy = await self._get_required_proxy(country_code, account_key or phone)

        profile = device_profile or FingerprintManager().generate_telegram_device_profile(account_key or phone)

        # Create temporary in-memory client; the persisted account stores a StringSession.
        client = TelegramClient(
            StringSession(),
            int(api_id),
            api_hash,
            proxy=proxy,
            device_model=profile["device_model"],
            system_version=profile["system_version"],
            app_version=profile["app_version"],
            lang_code=profile["lang_code"],
            system_lang_code=profile["system_lang_code"],
        )

        try:
            await client.connect()

            # Send code request
            sent_code = await client.send_code_request(phone)

            # Store login session
            session_id = f"{phone}_{sent_code.phone_code_hash}"
            login_session = LoginSession(
                phone=phone,
                api_id=api_id,
                api_hash=api_hash,
                phone_code_hash=sent_code.phone_code_hash,
                client=client,
                created_at=datetime.utcnow(),
                expires_at=datetime.utcnow() + timedelta(minutes=5),
                device_profile=profile,
            )
            self._login_sessions[session_id] = login_session

            self.logger.info(
                "verification_code_sent",
                phone=phone,
                session_id=session_id,
                code_type=sent_code.type.__class__.__name__,
            )

            return {
                "session_id": session_id,
                "phone_code_hash": sent_code.phone_code_hash,
                "code_type": sent_code.type.__class__.__name__,
                "timeout": 300,  # 5 minutes
                "device_profile": profile,
            }

        except FloodWaitError as e:
            self.logger.warning("rate_limited", phone=phone, wait_seconds=e.seconds)
            await client.disconnect()
            raise Exception(f"Rate limited. Please wait {e.seconds} seconds.")
        except Exception as e:
            self.logger.error("send_code_failed", phone=phone, error=str(e))
            await client.disconnect()
            raise

    async def verify_code(
        self,
        session_id: str,
        code: str,
    ) -> Dict[str, Any]:
        """
        Verify the code sent to phone.

        Args:
            session_id: Session ID from send_code
            code: Verification code from SMS/Telegram

        Returns:
            Dict with status and session info

        Raises:
            PhoneCodeInvalidError: Invalid code
            PhoneCodeExpiredError: Code expired
            SessionPasswordNeededError: 2FA required
        """
        self.logger.info("verifying_code", session_id=session_id)

        # Get login session
        login_session = self._login_sessions.get(session_id)
        if not login_session:
            raise Exception("Login session not found or expired")

        if login_session.is_expired():
            await self._cleanup_session(session_id)
            raise Exception("Login session expired. Please request a new code.")

        try:
            # Attempt sign in
            user = await login_session.client.sign_in(
                phone=login_session.phone,
                code=code,
                phone_code_hash=login_session.phone_code_hash,
            )

            # Success - save session
            session_string = self._export_session_string(login_session.client)
            user_id = user.id
            username = user.username

            self.logger.info(
                "login_successful",
                phone=login_session.phone,
                user_id=user_id,
                username=username,
            )

            # Cleanup
            await self._cleanup_session(session_id)

            return {
                "status": "success",
                "requires_2fa": False,
                "user_id": user_id,
                "username": username,
                "session_string": session_string,
                "device_profile": login_session.device_profile,
            }

        except SessionPasswordNeededError:
            # 2FA required
            self.logger.info("2fa_required", phone=login_session.phone)
            login_session.requires_2fa = True

            return {
                "status": "requires_2fa",
                "requires_2fa": True,
                "session_id": session_id,
                "device_profile": login_session.device_profile,
            }

        except PhoneCodeInvalidError:
            self.logger.warning("invalid_code", session_id=session_id)
            raise Exception("Invalid verification code")

        except PhoneCodeExpiredError:
            self.logger.warning("code_expired", session_id=session_id)
            await self._cleanup_session(session_id)
            raise Exception("Verification code expired. Please request a new code.")

        except Exception as e:
            self.logger.error("verify_code_failed", session_id=session_id, error=str(e))
            raise

    async def verify_2fa(
        self,
        session_id: str,
        password: str,
    ) -> Dict[str, Any]:
        """
        Verify 2FA password.

        Args:
            session_id: Session ID from verify_code
            password: 2FA password

        Returns:
            Dict with status and session info

        Raises:
            PasswordHashInvalidError: Invalid password
        """
        self.logger.info("verifying_2fa", session_id=session_id)

        # Get login session
        login_session = self._login_sessions.get(session_id)
        if not login_session:
            raise Exception("Login session not found or expired")

        if not login_session.requires_2fa:
            raise Exception("2FA not required for this session")

        if login_session.is_expired():
            await self._cleanup_session(session_id)
            raise Exception("Login session expired. Please start over.")

        try:
            # Sign in with password
            user = await login_session.client.sign_in(password=password)

            # Success - save session
            session_string = self._export_session_string(login_session.client)
            user_id = user.id
            username = user.username

            self.logger.info(
                "2fa_login_successful",
                phone=login_session.phone,
                user_id=user_id,
                username=username,
            )

            # Cleanup
            await self._cleanup_session(session_id)

            return {
                "status": "success",
                "user_id": user_id,
                "username": username,
                "session_string": session_string,
                "device_profile": login_session.device_profile,
            }

        except PasswordHashInvalidError:
            self.logger.warning("invalid_2fa_password", session_id=session_id)
            raise Exception("Invalid 2FA password")

        except Exception as e:
            self.logger.error("verify_2fa_failed", session_id=session_id, error=str(e))
            raise

    async def import_session(
        self,
        phone: str,
        session_file_path: str,
        api_id: str,
        api_hash: str,
        country_code: str = "US",
        account_key: Optional[str] = None,
        proxy: Optional[Dict[str, Any]] = None,
        proxy_required: Optional[bool] = None,
        device_profile: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Import existing session file.

        Args:
            phone: Phone number
            session_file_path: Path to .session file
            api_id: Telegram API ID
            api_hash: Telegram API Hash
            country_code: ISO 3166-1 alpha-2 country code for proxy targeting
            account_key: Stable account identifier for sticky proxy sessions
            proxy: Optional proxy configuration

        Returns:
            Dict with user info and session string

        Raises:
            Exception: If session is invalid or expired
        """
        self.logger.info("importing_session", phone=phone, file=session_file_path)

        if not os.path.exists(session_file_path):
            raise Exception("Session file not found")

        should_require_proxy = (
            getattr(self.settings, "PROMOTER_PROXY_REQUIRED", True)
            if proxy_required is None
            else proxy_required
        )
        if proxy is None and should_require_proxy:
            proxy = await self._get_required_proxy(country_code, account_key or phone)

        profile = device_profile or FingerprintManager().generate_telegram_device_profile(account_key or phone)

        # Create client with existing session
        client = TelegramClient(
            session_file_path.replace(".session", ""),
            int(api_id),
            api_hash,
            proxy=proxy,
            device_model=profile["device_model"],
            system_version=profile["system_version"],
            app_version=profile["app_version"],
            lang_code=profile["lang_code"],
            system_lang_code=profile["system_lang_code"],
        )

        try:
            await client.connect()

            # Check if session is valid
            if not await client.is_user_authorized():
                await client.disconnect()
                raise Exception("Session is not authorized. Please login again.")

            # Get user info
            me = await client.get_me()
            user_id = me.id
            username = me.username
            phone_from_session = me.phone

            # Verify phone matches
            if phone_from_session and phone_from_session != phone.replace("+", ""):
                self.logger.warning(
                    "phone_mismatch",
                    expected=phone,
                    actual=phone_from_session,
                )

            # Export session string
            session_string = self._export_session_string(client)

            await client.disconnect()

            self.logger.info(
                "session_imported",
                phone=phone,
                user_id=user_id,
                username=username,
            )

            return {
                "status": "success",
                "user_id": user_id,
                "username": username,
                "phone": f"+{phone_from_session}" if phone_from_session else phone,
                "session_string": session_string,
                "device_profile": profile,
            }

        except Exception as e:
            self.logger.error("import_session_failed", phone=phone, error=str(e))
            await client.disconnect()
            raise

    async def _cleanup_session(self, session_id: str) -> None:
        """Clean up temporary login session."""
        login_session = self._login_sessions.pop(session_id, None)
        if login_session:
            try:
                await login_session.client.disconnect()
            except Exception as e:
                self.logger.warning("cleanup_failed", session_id=session_id, error=str(e))

    async def cleanup_expired_sessions(self) -> None:
        """Clean up all expired login sessions."""
        expired = [
            sid for sid, session in self._login_sessions.items()
            if session.is_expired()
        ]
        for session_id in expired:
            await self._cleanup_session(session_id)

        if expired:
            self.logger.info("cleaned_expired_sessions", count=len(expired))
