"""
Unit Tests for Verification Manager
"""

import pytest
from unittest.mock import AsyncMock, patch
from datetime import datetime, timedelta

from app.modules.guardian.verification.verification_mgr import (
    VerificationManager,
    JoinResult,
    VerifyResult,
)
from app.modules.guardian.verification.captcha_gen import CaptchaGenerator, Captcha


class TestVerificationManager:
    """Tests for VerificationManager."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database session."""
        db = AsyncMock()
        return db

    @pytest.fixture
    def verification_manager(self, mock_db):
        """Create verification manager instance."""
        with patch('app.modules.guardian.verification.verification_mgr.get_guardian_config') as mock_config:
            mock_config.return_value.verification_timeout_minutes = 5
            mock_config.return_value.max_verification_attempts = 3
            manager = VerificationManager(mock_db)
        return manager

    def test_join_result_welcome(self):
        """Test JoinResult for welcome."""
        result = JoinResult(
            action="welcome",
            should_verify=False,
            message="Welcome!",
            session_id=None,
            verification_type=None
        )
        
        assert result.action == "welcome"
        assert result.should_verify is False
        assert result.message == "Welcome!"
        assert result.session_id is None

    def test_join_result_verify(self):
        """Test JoinResult for verification."""
        result = JoinResult(
            action="verify",
            should_verify=True,
            message="Please verify",
            session_id="abc123",
            verification_type="captcha"
        )
        
        assert result.action == "verify"
        assert result.should_verify is True
        assert result.session_id == "abc123"
        assert result.verification_type == "captcha"

    def test_verify_result_success(self):
        """Test VerifyResult for success."""
        result = VerifyResult(
            success=True,
            message="Verification passed",
            remaining_attempts=3
        )
        
        assert result.success is True
        assert result.message == "Verification passed"
        assert result.remaining_attempts == 3

    def test_verify_result_failure(self):
        """Test VerifyResult for failure."""
        result = VerifyResult(
            success=False,
            message="Wrong answer",
            remaining_attempts=2
        )
        
        assert result.success is False
        assert result.message == "Wrong answer"
        assert result.remaining_attempts == 2


class TestCaptchaGenerator:
    """Tests for CaptchaGenerator."""

    @pytest.fixture
    def generator(self):
        """Create captcha generator instance."""
        return CaptchaGenerator()

    def test_generate_code_length(self, generator):
        """Test captcha code length."""
        code = generator.generate_code()
        assert len(code) == CaptchaGenerator.CAPTCHA_LENGTH

    def test_generate_code_characters(self, generator):
        """Test captcha code uses valid characters."""
        code = generator.generate_code()
        valid_chars = set(CaptchaGenerator.CAPTCHA_CHARS)
        
        for char in code:
            assert char in valid_chars

    def test_generate_captcha(self, generator):
        """Test captcha generation."""
        captcha = generator.generate()
        
        assert captcha.code is not None
        assert len(captcha.code) == CaptchaGenerator.CAPTCHA_LENGTH
        assert captcha.image_data is None
        assert captcha.expires_at > datetime.utcnow()

    def test_generate_math_captcha(self, generator):
        """Test math captcha generation."""
        captcha = generator.generate_math_captcha()
        
        assert captcha.code is not None
        assert captcha.image_data is not None
        assert "+" in captcha.image_data
        assert captcha.expires_at > datetime.utcnow()

    def test_verify_correct(self, generator):
        """Test captcha verification with correct answer."""
        captcha = generator.generate()
        result = generator.verify(captcha.code, captcha.code)
        assert result is True

    def test_verify_incorrect(self, generator):
        """Test captcha verification with incorrect answer."""
        captcha = generator.generate()
        result = generator.verify(captcha.code, "WRONG")
        assert result is False

    def test_verify_case_insensitive(self, generator):
        """Test captcha verification is case insensitive."""
        captcha = generator.generate()
        result = generator.verify(captcha.code, captcha.code.lower())
        assert result is True

    def test_generate_session_id(self, generator):
        """Test session ID generation."""
        session_id = generator.generate_session_id()
        
        assert session_id is not None
        assert len(session_id) > 20

    def test_generate_session_id_unique(self, generator):
        """Test session IDs are unique."""
        ids = [generator.generate_session_id() for _ in range(100)]
        assert len(set(ids)) == 100


class TestCaptcha:
    """Tests for Captcha dataclass."""

    def test_captcha_creation(self):
        """Test Captcha creation."""
        expires = datetime.utcnow() + timedelta(minutes=5)
        captcha = Captcha(
            code="ABCD",
            image_data=None,
            expires_at=expires
        )
        
        assert captcha.code == "ABCD"
        assert captcha.image_data is None
        assert captcha.expires_at == expires

    def test_captcha_with_image(self):
        """Test Captcha with image data."""
        expires = datetime.utcnow() + timedelta(minutes=5)
        captcha = Captcha(
            code="8",
            image_data="5+3",
            expires_at=expires
        )
        
        assert captcha.code == "8"
        assert captcha.image_data == "5+3"
