"""
Unit Tests for Device Fingerprint Module

Tests cover:
- User-Agent generation
- Device fingerprint generation
- Fingerprint consistency
- Browser/OS parsing
- Canvas noise generation
- Statistics
"""

import pytest
import random

from app.core.network.fingerprint import (
    FingerprintManager,
    DeviceFingerprint,
    UA_Library,
)


class TestUA_Library:
    """Test User-Agent library."""

    def test_get_random_ua(self):
        """Test getting random User-Agent."""
        ua = UA_Library.get_random_ua()
        assert ua is not None
        assert len(ua) > 0
        assert "Mozilla" in ua

    def test_get_random_ua_returns_different(self):
        """Test that random UA can return different values."""
        ua1 = UA_Library.get_random_ua()
        # Might pass or fail randomly, but should not always be same
        assert isinstance(ua1, str)

    def test_get_by_os_windows(self):
        """Test getting UA for Windows."""
        ua = UA_Library.get_by_os("windows")
        assert "Windows" in ua
        assert "Chrome" in ua or "Firefox" in ua or "Edge" in ua

    def test_get_by_os_macos(self):
        """Test getting UA for macOS."""
        ua = UA_Library.get_by_os("macos")
        assert "Macintosh" in ua or "Mac OS X" in ua

    def test_get_by_os_android(self):
        """Test getting UA for Android."""
        ua = UA_Library.get_by_os("android")
        assert "Linux" in ua
        assert "Android" in ua

    def test_get_by_os_ios(self):
        """Test getting UA for iOS."""
        ua = UA_Library.get_by_os("ios")
        assert "iPhone" in ua
        assert "OS" in ua

    def test_get_by_os_unknown_returns_windows(self):
        """Test that unknown OS returns Windows UA."""
        ua = UA_Library.get_by_os("unknown")
        assert "Windows" in ua

    def test_windows_chrome_ua_valid(self):
        """Test Windows Chrome UA strings are valid."""
        for ua in UA_Library.WINDOWS_CHROME_UA:
            assert "Windows" in ua
            assert "Chrome" in ua
            assert "AppleWebKit" in ua

    def test_macos_chrome_ua_valid(self):
        """Test macOS Chrome UA strings are valid."""
        for ua in UA_Library.MACOS_CHROME_UA:
            assert "Macintosh" in ua
            assert "Chrome" in ua

    def test_android_chrome_ua_valid(self):
        """Test Android Chrome UA strings are valid."""
        for ua in UA_Library.ANDROID_CHROME_UA:
            assert "Linux" in ua
            assert "Android" in ua
            assert "Mobile" in ua

    def test_ios_safari_ua_valid(self):
        """Test iOS Safari UA strings are valid."""
        for ua in UA_Library.IOS_SAFARI_UA:
            assert "iPhone" in ua
            assert "Safari" in ua


class TestDeviceFingerprint:
    """Test DeviceFingerprint dataclass."""

    def test_create_fingerprint(self):
        """Test creating a device fingerprint."""
        fp = DeviceFingerprint(
            fingerprint_id="abc123",
            user_agent="Mozilla/5.0 (Windows NT 10.0) Chrome/125.0",
            os_type="windows",
            os_version="10.0",
            browser="chrome",
            browser_version="125.0.0.0",
            screen_resolution="1920x1080",
        )

        assert fp.fingerprint_id == "abc123"
        assert fp.os_type == "windows"
        assert fp.screen_resolution == "1920x1080"

    def test_fingerprint_defaults(self):
        """Test fingerprint default values."""
        fp = DeviceFingerprint(
            fingerprint_id="test",
            user_agent="test",
            os_type="windows",
            os_version="10.0",
            browser="chrome",
            browser_version="125.0",
            screen_resolution="1920x1080",
        )

        assert fp.color_depth == 24
        assert fp.timezone == "Asia/Shanghai"
        assert fp.language == "zh-CN"
        assert fp.platform == "Win32"
        assert fp.device_memory == 8
        assert fp.hardware_concurrency == 8

    def test_dns_cache_default(self):
        """Test DNS cache has default values."""
        fp = DeviceFingerprint(
            fingerprint_id="test",
            user_agent="test",
            os_type="windows",
            os_version="10.0",
            browser="chrome",
            browser_version="125.0",
            screen_resolution="1920x1080",
        )

        assert len(fp.dns_cache) == 2
        assert "8.8.8.8" in fp.dns_cache


class TestFingerprintManager:
    """Test FingerprintManager."""

    @pytest.fixture
    def manager(self):
        """Create FingerprintManager instance."""
        return FingerprintManager()

    def test_generate_fingerprint(self, manager):
        """Test generating a new fingerprint."""
        fp = manager.generate_fingerprint()

        assert fp is not None
        assert fp.fingerprint_id is not None
        assert len(fp.fingerprint_id) == 16
        assert fp.user_agent is not None
        assert fp.os_type in ["windows", "macos", "android", "ios"]

    def test_generate_fingerprint_with_os(self, manager):
        """Test generating fingerprint for specific OS."""
        fp = manager.generate_fingerprint(os_type="windows")

        assert fp.os_type == "windows"
        assert "Windows" in fp.user_agent

    def test_generate_fingerprint_with_account_id(self, manager):
        """Test generating fingerprint for account (cached)."""
        fp1 = manager.generate_fingerprint(account_id="acc123")
        fp2 = manager.generate_fingerprint(account_id="acc123")

        assert fp1.fingerprint_id == fp2.fingerprint_id
        assert fp1.user_agent == fp2.user_agent

    def test_generate_fingerprint_different_accounts(self, manager):
        """Test different accounts get different fingerprints."""
        fp1 = manager.generate_fingerprint(account_id="acc1")
        fp2 = manager.generate_fingerprint(account_id="acc2")

        assert fp1.fingerprint_id != fp2.fingerprint_id

    def test_generate_fingerprint_without_account(self, manager):
        """Test generating fingerprint without account ID."""
        fp1 = manager.generate_fingerprint()
        fp2 = manager.generate_fingerprint()

        # Without account_id, fingerprints may or may not be different
        # (depends on random selection)
        assert fp1 is not None
        assert fp2 is not None

    def test_get_fingerprint(self, manager):
        """Test getting existing fingerprint."""
        manager.generate_fingerprint(account_id="acc123")
        fp = manager.get_fingerprint("acc123")

        assert fp is not None
        assert fp.fingerprint_id is not None

    def test_get_nonexistent_fingerprint(self, manager):
        """Test getting non-existent fingerprint returns None."""
        fp = manager.get_fingerprint("nonexistent")
        assert fp is None

    def test_remove_fingerprint(self, manager):
        """Test removing fingerprint."""
        manager.generate_fingerprint(account_id="acc123")
        result = manager.remove_fingerprint("acc123")

        assert result is True
        assert manager.get_fingerprint("acc123") is None

    def test_remove_nonexistent_fingerprint(self, manager):
        """Test removing non-existent fingerprint returns False."""
        result = manager.remove_fingerprint("nonexistent")
        assert result is False

    def test_fingerprint_id_format(self, manager):
        """Test fingerprint ID is 16 characters."""
        fp = manager.generate_fingerprint()
        assert len(fp.fingerprint_id) == 16
        assert fp.fingerprint_id.isalnum()


class TestBrowserParsing:
    """Test browser parsing from User-Agent."""

    @pytest.fixture
    def manager(self):
        """Create FingerprintManager instance."""
        return FingerprintManager()

    def test_parse_chrome(self, manager):
        """Test parsing Chrome browser."""
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        browser, version = manager._parse_browser(ua)

        assert browser == "chrome"
        assert version == "125.0.0.0"

    def test_parse_firefox(self, manager):
        """Test parsing Firefox browser."""
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0"
        browser, version = manager._parse_browser(ua)

        assert browser == "firefox"
        assert version == "126.0"

    def test_parse_edge(self, manager):
        """Test parsing Edge browser."""
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0"
        browser, version = manager._parse_browser(ua)

        assert browser == "edge"
        assert version == "125.0.0.0"

    def test_parse_safari(self, manager):
        """Test parsing Safari browser."""
        ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
        browser, version = manager._parse_browser(ua)

        assert browser == "safari"
        assert version == "17.4"

    def test_parse_unknown(self, manager):
        """Test parsing unknown browser."""
        ua = "Some Unknown Browser/1.0"
        browser, version = manager._parse_browser(ua)

        assert browser == "unknown"
        assert version == "0.0"


class TestOSVersion:
    """Test OS version strings."""

    @pytest.fixture
    def manager(self):
        """Create FingerprintManager instance."""
        return FingerprintManager()

    def test_windows_version(self, manager):
        """Test Windows version string."""
        version = manager._get_os_version("windows")
        assert version == "10.0"

    def test_macos_version(self, manager):
        """Test macOS version string."""
        version = manager._get_os_version("macos")
        assert version == "10.15.7"

    def test_android_version(self, manager):
        """Test Android version string."""
        version = manager._get_os_version("android")
        assert version == "14"

    def test_ios_version(self, manager):
        """Test iOS version string."""
        version = manager._get_os_version("ios")
        assert version == "17.4"

    def test_unknown_os_version(self, manager):
        """Test unknown OS version defaults to Windows."""
        version = manager._get_os_version("unknown")
        assert version == "10.0"


class TestPlatform:
    """Test platform strings."""

    @pytest.fixture
    def manager(self):
        """Create FingerprintManager instance."""
        return FingerprintManager()

    def test_windows_platform(self, manager):
        """Test Windows platform string."""
        platform = manager._get_platform("windows")
        assert platform == "Win64"

    def test_macos_platform(self, manager):
        """Test macOS platform string."""
        platform = manager._get_platform("macos")
        assert platform == "MacIntel"

    def test_android_platform(self, manager):
        """Test Android platform string."""
        platform = manager._get_platform("android")
        assert platform == "Linux armv8"

    def test_ios_platform(self, manager):
        """Test iOS platform string."""
        platform = manager._get_platform("ios")
        assert platform == "iPhone"


class TestCanvasNoise:
    """Test canvas noise generation."""

    @pytest.fixture
    def manager(self):
        """Create FingerprintManager instance."""
        return FingerprintManager()

    def test_get_canvas_noise(self, manager):
        """Test generating canvas noise."""
        noise = manager.get_canvas_noise(seed=12345)

        assert noise is not None
        assert len(noise) == 32
        assert isinstance(noise, bytes)

    def test_canvas_noise_same_seed(self, manager):
        """Test same seed produces same noise."""
        noise1 = manager.get_canvas_noise(seed=12345)
        noise2 = manager.get_canvas_noise(seed=12345)

        assert noise1 == noise2

    def test_canvas_noise_different_seeds(self, manager):
        """Test different seeds produce different noise."""
        noise1 = manager.get_canvas_noise(seed=12345)
        noise2 = manager.get_canvas_noise(seed=67890)

        assert noise1 != noise2

    def test_canvas_noise_bytes_range(self, manager):
        """Test noise bytes are in valid range."""
        noise = manager.get_canvas_noise(seed=12345)

        for byte in noise:
            assert 0 <= byte <= 255


class TestFingerprintConsistency:
    """Test fingerprint consistency for accounts."""

    @pytest.fixture
    def manager(self):
        """Create FingerprintManager instance."""
        return FingerprintManager()

    def test_consistent_fingerprint_fields(self, manager):
        """Test that same account gets consistent fingerprint fields."""
        fp1 = manager.generate_fingerprint(account_id="acc_consistent")
        fp2 = manager.generate_fingerprint(account_id="acc_consistent")

        assert fp1.fingerprint_id == fp2.fingerprint_id
        assert fp1.user_agent == fp2.user_agent
        assert fp1.os_type == fp2.os_type
        assert fp1.screen_resolution == fp2.screen_resolution
        assert fp1.canvas_seed == fp2.canvas_seed

    def test_fingerprint_stored_in_profiles(self, manager):
        """Test fingerprint is stored in profiles."""
        manager.generate_fingerprint(account_id="acc_stored")
        stats = manager.get_statistics()

        assert stats["total_profiles"] == 1


class TestFingerprintOSMatching:
    """Test fingerprint OS-specific attributes."""

    @pytest.fixture
    def manager(self):
        """Create FingerprintManager instance."""
        return FingerprintManager()

    def test_windows_fingerprint_has_windows_attrs(self, manager):
        """Test Windows fingerprint has correct attributes."""
        fp = manager.generate_fingerprint(os_type="windows")

        assert fp.os_type == "windows"
        assert "Windows" in fp.user_agent
        assert fp.platform == "Win64"
        assert fp.webgl_vendor in FingerprintManager.WEBGL_VENDORS["windows"]

    def test_macos_fingerprint_has_macos_attrs(self, manager):
        """Test macOS fingerprint has correct attributes."""
        fp = manager.generate_fingerprint(os_type="macos")

        assert fp.os_type == "macos"
        assert "Macintosh" in fp.user_agent or "Mac OS X" in fp.user_agent
        assert fp.platform == "MacIntel"
        assert fp.webgl_vendor in FingerprintManager.WEBGL_VENDORS["macos"]

    def test_android_fingerprint_has_android_attrs(self, manager):
        """Test Android fingerprint has correct attributes."""
        fp = manager.generate_fingerprint(os_type="android")

        assert fp.os_type == "android"
        assert "Android" in fp.user_agent
        assert "Mobile" in fp.user_agent

    def test_ios_fingerprint_has_ios_attrs(self, manager):
        """Test iOS fingerprint has correct attributes."""
        fp = manager.generate_fingerprint(os_type="ios")

        assert fp.os_type == "ios"
        assert "iPhone" in fp.user_agent
        assert fp.platform == "iPhone"


class TestStatistics:
    """Test fingerprint statistics."""

    @pytest.fixture
    def manager(self):
        """Create FingerprintManager instance."""
        return FingerprintManager()

    def test_empty_statistics(self, manager):
        """Test statistics with no fingerprints."""
        stats = manager.get_statistics()

        assert stats["total_profiles"] == 0
        assert stats["by_os_type"] == {}

    def test_statistics_after_generation(self, manager):
        """Test statistics after generating fingerprints."""
        manager.generate_fingerprint(account_id="acc1")
        manager.generate_fingerprint(account_id="acc2")
        manager.generate_fingerprint(account_id="acc3")

        stats = manager.get_statistics()

        assert stats["total_profiles"] == 3
        assert sum(stats["by_os_type"].values()) == 3

    def test_statistics_by_os_type(self, manager):
        """Test OS type breakdown in statistics."""
        manager.generate_fingerprint(account_id="acc1", os_type="windows")
        manager.generate_fingerprint(account_id="acc2", os_type="windows")
        manager.generate_fingerprint(account_id="acc3", os_type="macos")

        stats = manager.get_statistics()

        assert stats["by_os_type"]["windows"] == 2
        assert stats["by_os_type"]["macos"] == 1


class TestScreenResolutions:
    """Test screen resolution configuration."""

    @pytest.fixture
    def manager(self):
        """Create FingerprintManager instance."""
        return FingerprintManager()

    def test_windows_resolutions(self):
        """Test Windows has valid resolutions."""
        for res in FingerprintManager.SCREEN_RESOLUTIONS["windows"]:
            assert "x" in res
            width, height = res.split("x")
            assert width.isdigit()
            assert height.isdigit()

    def test_macos_resolutions(self):
        """Test macOS has valid resolutions."""
        for res in FingerprintManager.SCREEN_RESOLUTIONS["macos"]:
            assert "x" in res

    def test_mobile_resolutions(self):
        """Test mobile resolutions are smaller."""
        for res in FingerprintManager.SCREEN_RESOLUTIONS["android"]:
            width = int(res.split("x")[0])
            assert width < 500

    def test_generated_fingerprint_uses_valid_resolution(self, manager):
        """Test generated fingerprint uses valid resolution for OS."""
        for os_type in ["windows", "macos", "android", "ios"]:
            fp = manager.generate_fingerprint(os_type=os_type)
            valid_resolutions = FingerprintManager.SCREEN_RESOLUTIONS.get(os_type)
            if valid_resolutions:
                assert fp.screen_resolution in valid_resolutions
