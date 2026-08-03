"""海报生成器 - 生成推广裂变海报"""
import hashlib
import os
import re
import qrcode
from io import BytesIO
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont
from loguru import logger


class PosterGenerator:
    """推广海报生成器"""

    DEFAULT_WIDTH = 800
    DEFAULT_HEIGHT = 1200
    DEFAULT_QR_SIZE = 200

    def __init__(
        self,
        output_dir: str = "./assets/posters",
        width: int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
        qr_size: int = DEFAULT_QR_SIZE
    ):
        self.output_dir = Path(output_dir)
        self.width = width
        self.height = height
        self.qr_size = qr_size
        self.output_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_filename_token(value: str, max_prefix_length: int = 24) -> str:
        """Build a filesystem-safe, stable token for user-provided values."""
        safe_prefix = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
        safe_prefix = safe_prefix[:max_prefix_length].strip("._-") or "link"
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
        return f"{safe_prefix}_{digest}"

    def generate_qr_code(self, data: str, size: int = None) -> Image.Image:
        """生成二维码"""
        if size is None:
            size = self.qr_size

        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=10,
            border=4,
        )
        qr.add_data(data)
        qr.make(fit=True)

        img = qr.make_image(fill_color="black", back_color="white")

        # 调整大小
        img = img.resize((size, size), Image.Resampling.LANCZOS)
        return img

    def add_text_with_outline(
        self,
        draw: ImageDraw.ImageDraw,
        position: tuple,
        text: str,
        font: ImageFont.FreeTypeFont,
        text_color: str = "white",
        outline_color: str = "black",
        outline_width: int = 2
    ):
        """添加带描边的文字"""
        x, y = position

        # 绘制描边
        for offset_x in range(-outline_width, outline_width + 1):
            for offset_y in range(-outline_width, outline_width + 1):
                if offset_x == 0 and offset_y == 0:
                    continue
                draw.text((x + offset_x, y + offset_y), text, font=font, fill=outline_color)

        # 绘制主文字
        draw.text(position, text, font=font, fill=text_color)

    async def generate_affiliate_poster(
        self,
        user_id: int,
        aff_link: str,
        username: Optional[str] = None,
        template_path: Optional[str] = None
    ) -> str:
        """生成推广海报"""
        try:
            # 创建画布
            poster = Image.new("RGB", (self.width, self.height), color="#1a1a2e")
            draw = ImageDraw.Draw(poster)

            # 尝试加载模板背景
            if template_path and os.path.exists(template_path):
                template = Image.open(template_path).resize((self.width, self.height))
                poster = template.convert("RGB")
                draw = ImageDraw.Draw(poster)

            # 绘制渐变背景效果
            self._draw_gradient_background(draw)

            # 绘制装饰元素
            self._draw_decorations(draw)

            # 绘制标题
            self._draw_title(draw)

            # 绘制用户信息
            self._draw_user_info(draw, username, user_id)

            # 绘制说明文字
            self._draw_description(draw)

            # 生成并粘贴二维码
            qr = self.generate_qr_code(aff_link, self.qr_size)
            qr_position = (
                (self.width - self.qr_size) // 2,
                self.height - self.qr_size - 100
            )
            poster.paste(qr, qr_position)

            # 在二维码下方添加提示
            self._draw_qr_hint(draw, qr_position)

            # 保存海报
            filename = f"poster_{user_id}_{self._safe_filename_token(aff_link)}.png"
            filepath = self.output_dir / filename
            poster.save(filepath, "PNG", quality=95)

            logger.info(f"海报生成成功: {filepath}")
            return str(filepath)

        except Exception as e:
            logger.error(f"海报生成失败: {e}")
            raise

    def _draw_gradient_background(self, draw: ImageDraw.ImageDraw):
        """绘制渐变背景"""
        for y in range(self.height):
            ratio = y / self.height
            r = int(26 + (52 - 26) * ratio)
            g = int(26 + (52 - 26) * ratio)
            b = int(46 + (90 - 46) * ratio)
            draw.line([(0, y), (self.width, y)], fill=(r, g, b))

    def _draw_decorations(self, draw: ImageDraw.ImageDraw):
        """绘制装饰元素"""
        # 绘制顶部装饰线
        draw.rectangle([50, 50, self.width - 50, 55], fill="#e94560")

        # 绘制底部装饰线
        draw.rectangle([50, self.height - 100, self.width - 50, self.height - 95], fill="#e94560")

    def _draw_title(self, draw: ImageDraw.ImageDraw):
        """绘制标题"""
        try:
            title_font = ImageFont.truetype("arial.ttf", 48)
            subtitle_font = ImageFont.truetype("arial.ttf", 24)
        except Exception:
            title_font = ImageFont.load_default()
            subtitle_font = ImageFont.load_default()

        title = "XBoard"
        subtitle = "全球高速节点 | 稳定翻墙 | 即插即用"

        title_bbox = draw.textbbox((0, 0), title, font=title_font)
        title_width = title_bbox[2] - title_bbox[0]
        title_x = (self.width - title_width) // 2

        self.add_text_with_outline(
            draw,
            (title_x, 100),
            title,
            title_font,
            text_color="#ffd700",
            outline_color="#1a1a2e"
        )

        subtitle_bbox = draw.textbbox((0, 0), subtitle, font=subtitle_font)
        subtitle_width = subtitle_bbox[2] - subtitle_bbox[0]
        subtitle_x = (self.width - subtitle_width) // 2

        draw.text((subtitle_x, 170), subtitle, font=subtitle_font, fill="#cccccc")

    def _draw_user_info(self, draw: ImageDraw.ImageDraw, username: Optional[str], user_id: int):
        """绘制用户信息"""
        try:
            info_font = ImageFont.truetype("arial.ttf", 28)
        except Exception:
            info_font = ImageFont.load_default()

        if username:
            user_text = f"推广员: @{username}"
        else:
            user_text = f"推广员ID: {user_id}"

        user_bbox = draw.textbbox((0, 0), user_text, font=info_font)
        user_width = user_bbox[2] - user_bbox[0]
        user_x = (self.width - user_width) // 2

        draw.text((user_x, 250), user_text, font=info_font, fill="#ffffff")

    def _draw_description(self, draw: ImageDraw.ImageDraw):
        """绘制描述文字"""
        try:
            desc_font = ImageFont.truetype("arial.ttf", 20)
        except Exception:
            desc_font = ImageFont.load_default()

        lines = [
            "扫码注册即享新人福利",
            "高速节点全球覆盖",
            "邀请好友最高返50%佣金"
        ]

        y_offset = 350
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=desc_font)
            text_width = bbox[2] - bbox[0]
            x = (self.width - text_width) // 2

            draw.text((x, y_offset), line, font=desc_font, fill="#e94560")
            y_offset += 40

    def _draw_qr_hint(self, draw: ImageDraw.ImageDraw, qr_position: tuple):
        """绘制二维码提示"""
        try:
            hint_font = ImageFont.truetype("arial.ttf", 22)
        except Exception:
            hint_font = ImageFont.load_default()

        hint_text = "长按扫码立即注册"
        bbox = draw.textbbox((0, 0), hint_text, font=hint_font)
        text_width = bbox[2] - bbox[0]
        x = (self.width - text_width) // 2

        y = qr_position[1] + self.qr_size + 20
        draw.text((x, y), hint_text, font=hint_font, fill="#ffffff")

    async def generate_simple_poster(
        self,
        user_id: int,
        aff_link: str,
        username: Optional[str] = None
    ) -> str:
        """生成简洁版海报（无模板）"""
        try:
            poster = Image.new("RGB", (self.width, self.height), color="#16213e")
            draw = ImageDraw.Draw(poster)

            # 生成二维码
            qr = self.generate_qr_code(aff_link, 250)
            qr_position = (275, 800)
            poster.paste(qr, qr_position)

            # 添加标题
            try:
                title_font = ImageFont.truetype("arial.ttf", 60)
                big_font = ImageFont.truetype("arial.ttf", 36)
                small_font = ImageFont.truetype("arial.ttf", 24)
            except Exception:
                title_font = ImageFont.load_default(size=60)
                big_font = ImageFont.load_default(size=36)
                small_font = ImageFont.load_default(size=24)

            # 标题
            draw.text((250, 150), "XBoard", font=title_font, fill="#00d9ff", anchor="mm")

            # 副标题
            draw.text((400, 250), "专业网络加速服务", font=big_font, fill="#ffffff", anchor="mm")

            # 用户信息
            if username:
                draw.text((400, 500), f"专属推广员: @{username}", font=small_font, fill="#ffd700", anchor="mm")
            else:
                draw.text((400, 500), f"专属推广员ID: {user_id}", font=small_font, fill="#ffd700", anchor="mm")

            # 提示
            draw.text((400, 1100), "扫码立即体验", font=big_font, fill="#00ff88", anchor="mm")

            # 保存
            filename = f"poster_{user_id}.png"
            filepath = self.output_dir / filename
            poster.save(filepath, "PNG")

            return str(filepath)

        except Exception as e:
            logger.error(f"生成简洁海报失败: {e}")
            raise
