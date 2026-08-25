"""
Captcha Generator

Generates verification captchas for group join.
Supports both text-based and image-based captchas.
"""

import random
import secrets
import io
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Tuple
from enum import Enum

import structlog
from PIL import Image, ImageDraw, ImageFont, ImageFilter

logger = structlog.get_logger()


class CaptchaType(str, Enum):
    """Captcha types."""
    TEXT = "text"
    MATH = "math"
    IMAGE = "image"
    ALPHANUMERIC = "alphanumeric"


@dataclass
class Captcha:
    """Captcha data."""
    code: str
    image_data: Optional[str]  # Base64 encoded image
    expires_at: datetime
    captcha_type: CaptchaType = CaptchaType.TEXT


class CaptchaGenerator:
    """
    Generates captcha codes for verification.

    Supports multiple captcha types:
    - Text-based codes (4 characters)
    - Math problems (simple addition)
    - Image-based codes (with distortion)
    """

    CAPTCHA_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    CAPTCHA_LENGTH = 4
    CAPTCHA_EXPIRE_MINUTES = 5
    IMAGE_WIDTH = 200
    IMAGE_HEIGHT = 80

    # Font settings
    FONT_SIZE = 40

    def __init__(self):
        """Initialize CaptchaGenerator."""
        self.logger = logger.bind(module="captcha_generator")
        self._font = None

    def _get_font(self) -> ImageFont.FreeTypeFont:
        """Get or create font object."""
        if self._font is None:
            try:
                # Try to use a system font
                self._font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", self.FONT_SIZE)
            except (OSError, IOError):
                # Fallback to default font
                self._font = ImageFont.load_default()
        return self._font

    def generate_code(self, length: int = None) -> str:
        """
        Generate a random captcha code.

        Args:
            length: Length of code (default: CAPTCHA_LENGTH)

        Returns:
            Random captcha code
        """
        length = length or self.CAPTCHA_LENGTH
        return ''.join(
            random.choices(self.CAPTCHA_CHARS, k=length)
        )

    def _generate_distortion(self, image: Image.Image) -> Image.Image:
        """Apply distortion effects to the image."""
        # Apply slight blur
        image = image.filter(ImageFilter.GaussianBlur(radius=0.5))

        # Add noise
        pixels = image.load()
        width, height = image.size
        for _ in range(width * height // 20):
            x = random.randint(0, width - 1)
            y = random.randint(0, height - 1)
            noise = random.randint(-30, 30)
            r, g, b = pixels[x, y]
            pixels[x, y] = (
                max(0, min(255, r + noise)),
                max(0, min(255, g + noise)),
                max(0, min(255, b + noise))
            )

        return image

    def _draw_lines(self, draw: ImageDraw.Draw, width: int, height: int, num_lines: int = 3) -> None:
        """Draw random lines for visual complexity."""
        for _ in range(num_lines):
            x1 = random.randint(0, width)
            y1 = random.randint(0, height)
            x2 = random.randint(0, width)
            y2 = random.randint(0, height)
            color = (
                random.randint(180, 220),
                random.randint(180, 220),
                random.randint(180, 220)
            )
            draw.line([(x1, y1), (x2, y2)], fill=color, width=2)

    def _create_image_captcha(self, code: str) -> Tuple[str, Image.Image]:
        """
        Create an image captcha with the given code.

        Args:
            code: The captcha code

        Returns:
            Tuple of (base64_image, PIL_Image)
        """
        # Create image with gradient background
        image = Image.new('RGB', (self.IMAGE_WIDTH, self.IMAGE_HEIGHT))
        draw = ImageDraw.Draw(image)

        # Draw gradient background
        for y in range(self.IMAGE_HEIGHT):
            color_value = 240 - int((y / self.IMAGE_HEIGHT) * 20)
            draw.rectangle([(0, y), (self.IMAGE_WIDTH, y + 1)], fill=(color_value, color_value + 5, 255))

        # Draw干扰 lines
        self._draw_lines(draw, self.IMAGE_WIDTH, self.IMAGE_HEIGHT, num_lines=4)

        # Calculate character positions
        char_width = self.IMAGE_WIDTH // (len(code) + 1)
        x_start = char_width // 2

        # Draw each character with rotation
        font = self._get_font()
        for i, char in enumerate(code):
            x = x_start + i * char_width + random.randint(-5, 5)
            y = random.randint(10, 20)
            rotation = random.randint(-25, 25)

            # Create temporary image for rotation
            char_img = Image.new('RGBA', (50, 50), (255, 255, 255, 0))
            char_draw = ImageDraw.Draw(char_img)

            # Use different colors for each character
            color = (
                random.randint(20, 60),
                random.randint(20, 80),
                random.randint(100, 200)
            )

            char_draw.text((15, 5), char, font=font, fill=color)

            # Rotate and paste
            rotated = char_img.rotate(rotation, expand=1)
            image.paste(rotated, (x, y), rotated)

        # Apply distortion
        image = self._generate_distortion(image)

        return image

    def generate(self, captcha_type: CaptchaType = CaptchaType.TEXT) -> Captcha:
        """
        Generate a captcha.

        Args:
            captcha_type: Type of captcha to generate

        Returns:
            Captcha object with code and optional image
        """
        if captcha_type == CaptchaType.MATH:
            return self.generate_math_captcha()
        elif captcha_type == CaptchaType.IMAGE:
            return self.generate_image_captcha()
        elif captcha_type == CaptchaType.ALPHANUMERIC:
            return self.generate_alphanumeric_captcha()
        else:
            return self._generate_text_captcha()

    def _generate_text_captcha(self) -> Captcha:
        """Generate a text-based captcha."""
        code = self.generate_code()

        self.logger.debug("captcha_generated", code_length=len(code), type="text")

        return Captcha(
            code=code,
            image_data=None,
            expires_at=datetime.utcnow() + timedelta(minutes=self.CAPTCHA_EXPIRE_MINUTES),
            captcha_type=CaptchaType.TEXT
        )

    def generate_image_captcha(self, length: int = None) -> Captcha:
        """
        Generate an image-based captcha with visual distortion.

        Args:
            length: Length of captcha code

        Returns:
            Captcha object with base64 encoded image
        """
        length = length or self.CAPTCHA_LENGTH
        code = self.generate_code(length)

        # Create image
        image = self._create_image_captcha(code)

        # Convert to base64
        buffer = io.BytesIO()
        image.save(buffer, format='PNG', quality=85)
        image_bytes = buffer.getvalue()
        image_base64 = base64.b64encode(image_bytes).decode('utf-8')

        self.logger.debug("image_captcha_generated", code_length=len(code))

        return Captcha(
            code=code,
            image_data=f"data:image/png;base64,{image_base64}",
            expires_at=datetime.utcnow() + timedelta(minutes=self.CAPTCHA_EXPIRE_MINUTES),
            captcha_type=CaptchaType.IMAGE
        )

    def generate_math_captcha(self) -> Captcha:
        """
        Generate a math-based captcha.

        Returns:
            Captcha object with math problem
        """
        num1 = random.randint(1, 9)
        num2 = random.randint(1, 9)
        answer = num1 + num2

        code = f"{num1}+{num2}"

        self.logger.debug("math_captcha_generated", problem=code)

        return Captcha(
            code=str(answer),
            image_data=code,
            expires_at=datetime.utcnow() + timedelta(minutes=self.CAPTCHA_EXPIRE_MINUTES),
            captcha_type=CaptchaType.MATH
        )

    def generate_alphanumeric_captcha(self, length: int = 6) -> Captcha:
        """
        Generate an alphanumeric captcha.

        Args:
            length: Length of captcha code

        Returns:
            Captcha object
        """
        code = ''.join(
            random.choices(self.CAPTCHA_CHARS + "0123456789", k=length)
        )

        self.logger.debug("alphanumeric_captcha_generated", length=length)

        return Captcha(
            code=code,
            image_data=None,
            expires_at=datetime.utcnow() + timedelta(minutes=self.CAPTCHA_EXPIRE_MINUTES),
            captcha_type=CaptchaType.ALPHANUMERIC
        )

    def verify(self, code: str, answer: str) -> bool:
        """
        Verify a captcha answer.

        Args:
            code: The correct captcha code
            answer: User's answer

        Returns:
            True if answer matches
        """
        return code.upper().strip() == answer.upper().strip()

    def verify_math(self, correct_answer: str, user_answer: str) -> bool:
        """
        Verify a math captcha answer.

        Args:
            correct_answer: The correct answer
            user_answer: User's answer

        Returns:
            True if answer matches
        """
        try:
            return int(correct_answer) == int(user_answer.strip())
        except ValueError:
            return False

    def generate_session_id(self) -> str:
        """
        Generate a unique session ID.

        Returns:
            Session ID string
        """
        return secrets.token_urlsafe(32)
