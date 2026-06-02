"""
Device Fingerprint Module

Provides device fingerprint simulation for Telegram account protection.

Features:
- User-Agent generation
- Device fingerprint generation
- WebGL/Canvas noise
- TLS fingerprint simulation
"""

import random
from dataclasses import dataclass, field
from typing import Optional

import structlog

logger = structlog.get_logger()


@dataclass
class DeviceFingerprint:
    """
    Device fingerprint data.

    Attributes:
        fingerprint_id: Unique fingerprint identifier
        user_agent: Browser User-Agent string
        os_type: Operating system type
        os_version: OS version
        browser: Browser name
        browser_version: Browser version
        screen_resolution: Screen resolution
        color_depth: Color depth
        timezone: Timezone
        language: Language
        platform: Platform string
        device_memory: Device memory in GB
        hardware_concurrency: CPU cores
        canvas_seed: Canvas noise seed
        webgl_vendor: WebGL vendor
        webgl_renderer: WebGL renderer
        dns_cache: DNS cache servers
    """

    fingerprint_id: str
    user_agent: str
    os_type: str
    os_version: str
    browser: str
    browser_version: str
    screen_resolution: str
    color_depth: int = 24
    timezone: str = "Asia/Shanghai"
    language: str = "zh-CN"
    platform: str = "Win32"
    device_memory: int = 8
    hardware_concurrency: int = 8
    canvas_seed: int = 0
    webgl_vendor: str = "Intel Inc."
    webgl_renderer: str = "Intel Iris OpenGL Engine"
    dns_cache: list[str] = field(default_factory=lambda: ["8.8.8.8", "114.114.114.114"])


class UA_Library:
    """
    User-Agent library for realistic browser fingerprints.

    Provides a collection of real-world User-Agent strings for
    different browsers, operating systems, and devices.
    """

    # Chrome on Windows
    WINDOWS_CHROME_UA = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    ]

    # Chrome on macOS
    MACOS_CHROME_UA = [
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    ]

    # Safari on macOS
    MACOS_SAFARI_UA = [
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    ]

    # Chrome on Android
    ANDROID_CHROME_UA = [
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 13; SM-G998B) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    ]

    # Safari on iOS
    IOS_SAFARI_UA = [
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_3 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.3 Mobile/15E148 Safari/604.1",
    ]

    # Firefox on Windows
    WINDOWS_FIREFOX_UA = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) "
        "Gecko/20100101 Firefox/126.0",
    ]

    # Edge on Windows
    WINDOWS_EDGE_UA = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    ]

    @classmethod
    def get_random_ua(cls) -> str:
        """Get a random User-Agent."""
        all_ua = (
            cls.WINDOWS_CHROME_UA
            + cls.MACOS_CHROME_UA
            + cls.MACOS_SAFARI_UA
            + cls.ANDROID_CHROME_UA
            + cls.IOS_SAFARI_UA
            + cls.WINDOWS_FIREFOX_UA
            + cls.WINDOWS_EDGE_UA
        )
        return random.choice(all_ua)

    @classmethod
    def get_by_os(cls, os_type: str) -> str:
        """Get User-Agent by OS type."""
        if os_type == "windows":
            return random.choice(cls.WINDOWS_CHROME_UA)
        elif os_type == "macos":
            return random.choice(cls.MACOS_CHROME_UA + cls.MACOS_SAFARI_UA)
        elif os_type == "android":
            return random.choice(cls.ANDROID_CHROME_UA)
        elif os_type == "ios":
            return random.choice(cls.IOS_SAFARI_UA)
        return random.choice(cls.WINDOWS_CHROME_UA)


class FingerprintManager:
    """
    Device fingerprint manager.

    Generates and manages device fingerprints for Telegram accounts.
    Each account gets a consistent fingerprint to avoid detection.
    """

    # Screen resolutions by OS
    SCREEN_RESOLUTIONS = {
        "windows": ["1920x1080", "2560x1440", "1366x768", "1536x864", "1440x900"],
        "macos": ["2560x1600", "2880x1800", "1920x1200", "1680x1050"],
        "android": ["412x915", "360x800", "414x896", "393x873"],
        "ios": ["390x844", "428x926", "375x812", "414x896"],
    }

    # Device memory options (GB)
    DEVICE_MEMORY = [4, 8, 16]

    # CPU core options
    HARDWARE_CONCURRENCY = [4, 8, 16]

    # WebGL vendors by OS
    WEBGL_VENDORS = {
        "windows": ["Intel Inc.", "NVIDIA Corporation", "AMD"],
        "macos": ["Intel Inc.", "Apple Inc."],
        "android": ["Qualcomm", "ARM", "Mali"],
        "ios": ["Apple Inc."],
    }

    # WebGL renderers by OS
    WEBGL_RENDERERS = {
        "windows": [
            "Intel Iris OpenGL Engine",
            "NVIDIA GeForce GTX 1060 OpenGL Engine",
            "AMD Radeon Pro 5500M OpenGL Engine",
        ],
        "macos": [
            "Intel Iris OpenGL Engine",
            "Apple GPU",
        ],
        "android": [
            "Adreno (TM) 650",
            "Mali-G77",
            "Mali-G72",
        ],
        "ios": ["Apple GPU"],
    }

    def __init__(self):
        """Initialize FingerprintManager."""
        self._ua_library = UA_Library()
        self._profiles: dict[str, DeviceFingerprint] = {}
        self.logger = logger.bind(module="fingerprint_manager")

    def generate_fingerprint(
        self,
        account_id: Optional[str] = None,
        os_type: Optional[str] = None,
    ) -> DeviceFingerprint:
        """
        Generate a device fingerprint.

        Args:
            account_id: Account ID for consistency
            os_type: Specific OS type (random if not specified)

        Returns:
            DeviceFingerprint instance
        """
        if account_id and account_id in self._profiles:
            return self._profiles[account_id]

        if os_type is None:
            os_type = random.choice(["windows", "macos", "android", "ios"])

        ua = self._ua_library.get_by_os(os_type)

        browser, browser_version = self._parse_browser(ua)
        os_version = self._get_os_version(os_type)
        platform = self._get_platform(os_type)

        fingerprint = DeviceFingerprint(
            fingerprint_id=self._generate_id(account_id),
            user_agent=ua,
            os_type=os_type,
            os_version=os_version,
            browser=browser,
            browser_version=browser_version,
            screen_resolution=random.choice(self.SCREEN_RESOLUTIONS.get(os_type, ["1920x1080"])),
            platform=platform,
            device_memory=random.choice(self.DEVICE_MEMORY),
            hardware_concurrency=random.choice(self.HARDWARE_CONCURRENCY),
            canvas_seed=random.randint(100000, 999999),
            webgl_vendor=random.choice(self.WEBGL_VENDORS.get(os_type, ["Intel Inc."])),
            webgl_renderer=random.choice(self.WEBGL_RENDERERS.get(os_type, ["Intel Iris OpenGL Engine"])),
        )

        if account_id:
            self._profiles[account_id] = fingerprint

        self.logger.debug(
            "fingerprint_generated",
            fingerprint_id=fingerprint.fingerprint_id,
            os_type=os_type,
        )

        return fingerprint

    def get_fingerprint(self, account_id: str) -> Optional[DeviceFingerprint]:
        """
        Get existing fingerprint for account.

        Args:
            account_id: Account ID

        Returns:
            DeviceFingerprint or None
        """
        return self._profiles.get(account_id)

    def remove_fingerprint(self, account_id: str) -> bool:
        """
        Remove fingerprint for account.

        Args:
            account_id: Account ID

        Returns:
            True if removed
        """
        if account_id in self._profiles:
            del self._profiles[account_id]
            return True
        return False

    def _generate_id(self, account_id: Optional[str]) -> str:
        """Generate fingerprint ID."""
        import hashlib
        import time

        base = f"{account_id}:{time.time()}" if account_id else str(time.time())
        return hashlib.md5(base.encode()).hexdigest()[:16]

    def _parse_browser(self, ua: str) -> tuple[str, str]:
        """Parse browser info from User-Agent."""
        if "Edg/" in ua:
            version_match = ua.split("Edg/")[1].split()[0]
            return "edge", version_match
        elif "Chrome/" in ua:
            version_match = ua.split("Chrome/")[1].split()[0]
            return "chrome", version_match
        elif "Firefox/" in ua:
            version_match = ua.split("Firefox/")[1].split()[0]
            return "firefox", version_match
        elif "Safari/" in ua and "Chrome" not in ua:
            version_match = ua.split("Version/")[1].split()[0]
            return "safari", version_match
        return "unknown", "0.0"

    def _get_os_version(self, os_type: str) -> str:
        """Get OS version string."""
        versions = {
            "windows": "10.0",
            "macos": "10.15.7",
            "android": "14",
            "ios": "17.4",
        }
        return versions.get(os_type, "10.0")

    def _get_platform(self, os_type: str) -> str:
        """Get platform string."""
        platforms = {
            "windows": "Win64",
            "macos": "MacIntel",
            "android": "Linux armv8",
            "ios": "iPhone",
        }
        return platforms.get(os_type, "Win64")

    def get_canvas_noise(self, seed: int) -> bytes:
        """
        Generate canvas noise based on seed.

        Args:
            seed: Canvas seed

        Returns:
            Noise bytes
        """
        random.seed(seed)
        return bytes([random.randint(0, 255) for _ in range(32)])

    def get_statistics(self) -> dict:
        """Get fingerprint statistics."""
        return {
            "total_profiles": len(self._profiles),
            "by_os_type": self._count_by_os(),
        }

    def _count_by_os(self) -> dict:
        """Count fingerprints by OS type."""
        counts = {}
        for fp in self._profiles.values():
            counts[fp.os_type] = counts.get(fp.os_type, 0) + 1
        return counts
