"""单元测试 - 海报生成器"""
import pytest
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.utils.poster import PosterGenerator


class TestPosterGenerator:
    """测试海报生成器"""

    def setup_method(self):
        self.output_dir = Path(__file__).parent.parent.parent / "assets" / "test_posters"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.poster = PosterGenerator(
            output_dir=str(self.output_dir),
            width=800,
            height=1200,
            qr_size=200
        )

    def teardown_method(self):
        """清理测试文件"""
        import shutil
        if self.output_dir.exists():
            shutil.rmtree(self.output_dir)

    def test_generate_qr_code(self):
        """测试二维码生成"""
        qr = self.poster.generate_qr_code("https://example.com", size=100)

        assert qr is not None
        assert qr.size == (100, 100)

    def test_generate_qr_code_default_size(self):
        """测试默认大小二维码"""
        qr = self.poster.generate_qr_code("https://example.com")

        assert qr is not None
        assert qr.size == (200, 200)  # 默认 qr_size=200

    @pytest.mark.asyncio
    async def test_generate_affiliate_poster(self):
        """测试推广海报生成"""
        poster_path = await self.poster.generate_affiliate_poster(
            user_id=123456789,
            aff_link="https://xboard.com/register?aff=ABC123",
            username="testuser"
        )

        assert poster_path is not None
        assert Path(poster_path).exists()
        assert poster_path.endswith(".png")

    @pytest.mark.asyncio
    async def test_generate_simple_poster(self):
        """测试简洁版海报生成"""
        poster_path = await self.poster.generate_simple_poster(
            user_id=123456789,
            aff_link="https://xboard.com/register?aff=ABC123",
            username="testuser"
        )

        assert poster_path is not None
        assert Path(poster_path).exists()

    @pytest.mark.asyncio
    async def test_generate_poster_without_username(self):
        """测试无用户名海报生成"""
        poster_path = await self.poster.generate_simple_poster(
            user_id=123456789,
            aff_link="https://xboard.com/register?aff=ABC123",
            username=None
        )

        assert poster_path is not None
        assert Path(poster_path).exists()

    def test_output_directory_creation(self):
        """测试输出目录创建"""
        test_dir = Path(__file__).parent.parent.parent / "assets" / "new_test_dir"

        if test_dir.exists():
            import shutil
            shutil.rmtree(test_dir)

        poster = PosterGenerator(output_dir=str(test_dir))
        assert test_dir.exists()
