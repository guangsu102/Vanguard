"""
URL Builder Module

Builds tracking URLs with parameters and encryption.
"""

import base64
import hashlib
import json
import urllib.parse
from dataclasses import dataclass
from typing import Optional

import structlog

from app.modules.acquisition.config import TrackingConfig

logger = structlog.get_logger()


@dataclass
class URLBuilderConfig:
    """Configuration for URL building."""
    base_url: str = "https://xboard.com"
    encryption_key: Optional[str] = None
    code_expiry_days: int = 7


class URLBuilder:
    """
    Tracking URL builder with encryption support.

    Builds registration URLs with tracking parameters
    and optional encryption for the tracking code.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        config: Optional[TrackingConfig] = None,
    ):
        """
        Initialize URLBuilder.

        Args:
            base_url: Base URL for registration page
            config: Optional tracking configuration
        """
        self.config = config or TrackingConfig()
        self.base_url = base_url or self.config.base_url
        self.encryption_key = self.config.encryption_key
        self.encryption_enabled = self.config.encryption_enabled
        self.logger = logger.bind(module="url_builder")

    async def build_tracking_url(
        self,
        tracking_code: str,
        source_type: str = "tg_private",
        campaign: Optional[str] = None,
        group_id: Optional[int] = None,
        keyword: Optional[str] = None,
        bot_id: Optional[str] = None,
    ) -> str:
        """
        Build a tracking URL with parameters.

        Args:
            tracking_code: Tracking code
            source_type: Source type
            campaign: Campaign name
            group_id: Group ID
            keyword: Keyword
            bot_id: Bot ID

        Returns:
            Complete tracking URL
        """
        params = {
            "source": source_type,
            "ref": tracking_code,
        }

        if campaign:
            params["campaign"] = campaign
        if group_id:
            params["group_id"] = str(group_id)
        if keyword:
            params["keyword"] = keyword
        if bot_id:
            params["bot_id"] = bot_id

        query_string = urllib.parse.urlencode(params)
        return f"{self.base_url}/register?{query_string}"

    async def build_encrypted_url(
        self,
        tracking_code: str,
        source_type: str = "tg_private",
        **kwargs,
    ) -> str:
        """
        Build an encrypted tracking URL.

        Args:
            tracking_code: Tracking code
            source_type: Source type
            **kwargs: Additional parameters

        Returns:
            Encrypted tracking URL
        """
        if not self.encryption_key:
            return await self.build_tracking_url(tracking_code, source_type, **kwargs)

        # 加密追踪码
        encrypted_code = self._encrypt(tracking_code)

        # 构建参数
        params = {
            "source": source_type,
            "ref": encrypted_code,
            "type": "enc",
        }

        params.update(kwargs)

        query_string = urllib.parse.urlencode(params)
        return f"{self.base_url}/register?{query_string}"

    async def build_invite_url(
        self,
        user_id: int,
        source: str = "invite",
        campaign: Optional[str] = None,
    ) -> str:
        """
        Build an invite URL for a specific user.

        Args:
            user_id: User ID
            source: Source identifier
            campaign: Optional campaign

        Returns:
            Invite URL
        """
        tracking_code = f"inv_{user_id}_{source}"

        return await self.build_tracking_url(
            tracking_code=tracking_code,
            source_type="tg_invite",
            campaign=campaign,
        )

    async def build_deep_link(
        self,
        action: str,
        params: Optional[dict] = None,
    ) -> str:
        """
        Build a deep link for app navigation.

        Args:
            action: Action identifier
            params: Optional parameters

        Returns:
            Deep link URL
        """
        link_params = {"action": action}
        if params:
            link_params.update(params)

        encoded = urllib.parse.quote(json.dumps(link_params))
        return f"xboard://{action}?data={encoded}"

    async def parse_tracking_params(
        self,
        url: str,
    ) -> dict:
        """
        Parse tracking parameters from URL.

        Args:
            url: URL to parse

        Returns:
            Dict of tracking parameters
        """
        try:
            parsed = urllib.parse.urlparse(url)
            params = urllib.parse.parse_qs(parsed.query)

            result = {}
            if "source" in params:
                result["source_type"] = params["source"][0]
            if "ref" in params:
                ref = params["ref"][0]
                if params.get("type", [None])[0] == "enc" and self.encryption_key:
                    result["tracking_code"] = self._decrypt(ref)
                else:
                    result["tracking_code"] = ref
            if "campaign" in params:
                result["campaign"] = params["campaign"][0]
            if "group_id" in params:
                result["group_id"] = int(params["group_id"][0])
            if "keyword" in params:
                result["keyword"] = params["keyword"][0]
            if "bot_id" in params:
                result["bot_id"] = params["bot_id"][0]

            return result

        except Exception as e:
            self.logger.error("parse_tracking_params_error", url=url, error=str(e))
            return {}

    def validate_tracking_code(self, code: str) -> bool:
        """
        Validate a tracking code format.

        Args:
            code: Tracking code to validate

        Returns:
            True if valid format
        """
        if not code:
            return False

        # 检查格式：acq_userid_uuid 或 inv_userid_source
        if code.startswith("acq_") or code.startswith("inv_"):
            return len(code) <= 100

        return False

    def _encrypt(self, text: str) -> str:
        """Encrypt text using base64 + hash."""
        if not self.encryption_key:
            return text

        combined = f"{text}:{self.encryption_key}"
        hash_val = hashlib.sha256(combined.encode()).hexdigest()[:8]

        data = f"{text}:{hash_val}"
        encoded = base64.urlsafe_b64encode(data.encode()).decode()
        return encoded

    def _decrypt(self, encrypted: str) -> str:
        """Decrypt encrypted text."""
        if not self.encryption_key:
            return encrypted

        try:
            decoded = base64.urlsafe_b64decode(encrypted.encode()).decode()
            text, expected_hash = decoded.rsplit(":", 1)

            combined = f"{text}:{self.encryption_key}"
            hash_val = hashlib.sha256(combined.encode()).hexdigest()[:8]

            if hash_val == expected_hash:
                return text

            return encrypted

        except Exception as e:
            self.logger.error("decrypt_error", error=str(e))
            return encrypted

    async def generate_short_code(self, tracking_code: str) -> str:
        """
        Generate a short code for compact URLs.

        Args:
            tracking_code: Full tracking code

        Returns:
            Short code (8 characters)
        """
        hash_val = hashlib.md5(tracking_code.encode()).hexdigest()
        return hash_val[:8]

    async def build_short_url(
        self,
        tracking_code: str,
        short_domain: Optional[str] = None,
    ) -> str:
        """
        Build a short URL.

        Args:
            tracking_code: Tracking code
            short_domain: Optional short domain

        Returns:
            Short URL
        """
        short_code = await self.generate_short_code(tracking_code)
        domain = short_domain or "xbd.link"
        return f"https://{domain}/{short_code}"
