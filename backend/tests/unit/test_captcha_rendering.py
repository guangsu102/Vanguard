"""Real Pillow regression for fontless production images and unclipped glyphs."""
import base64
import io
from datetime import datetime, timedelta

import pytest
from PIL import Image, ImageFont

from app.modules.guardian.verification.captcha_gen import CaptchaGenerator


@pytest.fixture
def no_system_font(monkeypatch):
    original = ImageFont.truetype

    def without_system_font(font, *args, **kwargs):
        if isinstance(font, str) and font.startswith('/usr/share/fonts/'):
            raise OSError('No system font in container')
        return original(font, *args, **kwargs)

    monkeypatch.setattr(ImageFont, 'truetype', without_system_font)


def test_fontless_fallback_honors_40px_and_safe_probe(no_system_font):
    generator = CaptchaGenerator()
    font = generator._get_font()
    assert isinstance(font, ImageFont.FreeTypeFont)
    assert font.size == 40
    probe = generator.verify_rendering()
    assert probe == {'version': 'pp-ai-readable-captcha-v2', 'font_size': 40,
                     'font_height': min(font.getbbox(c)[3]-font.getbbox(c)[1]
                                        for c in generator.CAPTCHA_CHARS),
                     'width': 320, 'height': 120}
    assert probe['font_height'] >= 24
    assert set(probe) == {'version', 'font_size', 'font_height', 'width', 'height'}


@pytest.mark.parametrize('angle', [-12, 0, 12])
@pytest.mark.parametrize('char', CaptchaGenerator.CAPTCHA_CHARS)
def test_all_actual_glyphs_have_large_ink_and_no_clipped_edges(no_system_font, char, angle):
    glyph = CaptchaGenerator()._render_character(char, angle, (30, 40, 80))
    alpha = glyph.getchannel('A')
    bounds = alpha.getbbox()
    assert bounds is not None
    left, top, right, bottom = bounds
    assert left > 0 and top > 0 and right < glyph.width and bottom < glyph.height
    assert bottom-top >= 24
    assert sum(pixel > 32 for pixel in alpha.getdata()) >= 130


def test_all_characters_paste_inside_slot_and_canvas(no_system_font, monkeypatch):
    original = Image.Image.paste
    positions = []

    def capture(image, glyph, box, mask=None):
        if isinstance(glyph, Image.Image) and glyph.mode == 'RGBA':
            assert glyph.getchannel('A').getbbox()
            assert box[0] >= 8 and box[1] >= 8
            assert box[0]+glyph.width <= image.width-8
            assert box[1]+glyph.height <= image.height-8
            positions.append((box[0], glyph.width))
        return original(image, glyph, box, mask)

    monkeypatch.setattr(Image.Image, 'paste', capture)
    generator = CaptchaGenerator()
    for number in range(0, len(generator.CAPTCHA_CHARS), 4):
        text = generator.CAPTCHA_CHARS[number:number+4].ljust(4, 'W')
        before = len(positions)
        image = generator._create_image_captcha(text)
        assert image.size == (320, 120)
        current = positions[before:]
        assert len(current) == 4
        assert all(current[i][0]+current[i][1] < current[i+1][0] for i in range(3))


def test_real_png_remains_four_chars_and_five_minutes(no_system_font):
    before = datetime.utcnow()
    captcha = CaptchaGenerator().generate_image_captcha()
    assert len(captcha.code) == 4
    assert all(char in CaptchaGenerator.CAPTCHA_CHARS for char in captcha.code)
    assert before+timedelta(minutes=5) <= captcha.expires_at <= datetime.utcnow()+timedelta(minutes=5)
    png = base64.b64decode(captcha.image_data.split(',', 1)[1])
    assert png.startswith(b'\x89PNG\r\n\x1a\n')
    with Image.open(io.BytesIO(png)) as image:
        assert image.size == (320, 120)
        assert image.mode == 'RGB'


def test_unsupported_or_ignored_size_fails_closed(no_system_font, monkeypatch):
    monkeypatch.setattr(ImageFont, 'load_default', lambda **kwargs: object())
    with pytest.raises(RuntimeError, match='font size was not honored'):
        CaptchaGenerator().generate_image_captcha()


def test_old_pillow_without_sized_default_fails_closed(no_system_font, monkeypatch):
    def old_default():
        raise AssertionError('Unscaled default must not be requested')
    monkeypatch.setattr(ImageFont, 'load_default', old_default)
    with pytest.raises(RuntimeError, match='font is unavailable'):
        CaptchaGenerator().generate_image_captcha()
