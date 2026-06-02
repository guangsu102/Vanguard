"""单元测试 - 内容模板"""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.utils.content import ContentTemplates


class TestContentTemplates:
    """测试文案模板"""

    def setup_method(self):
        self.templates = ContentTemplates()

    def test_get_checkin_success(self):
        """测试签到成功文案"""
        result = self.templates.get_checkin_success(
            name="测试用户",
            bonus_mb=512,
            is_special=False
        )

        assert "测试用户" in result
        assert "512" in result or "MB" in result
        assert "签到" in result or "成功" in result

    def test_get_checkin_success_special(self):
        """测试特殊奖励文案"""
        result = self.templates.get_checkin_success(
            name="欧皇",
            bonus_mb=2048,
            is_special=True
        )

        assert "欧皇" in result
        assert "2048" in result
        # 特殊奖励应该包含双倍相关的词汇
        assert any(word in result for word in ["双倍", "倍", "惊喜", "欧皇"])

    def test_get_abandoned_cart_message(self):
        """测试弃单挽回文案"""
        result = self.templates.get_abandoned_cart_message(
            order_id="ORD123456",
            amount="99.00",
            coupon_code="SAVE20",
            discount=20,
            validity_hours=2
        )

        assert "ORD123456" in result
        assert "SAVE20" in result
        assert "20%" in result or "20" in result
        assert "2" in result  # 有效期

    def test_get_welcome_message(self):
        """测试欢迎消息"""
        result = self.templates.get_welcome_message()

        assert "欢迎" in result
        assert "XBoard" in result

    def test_get_help_message(self):
        """测试帮助消息"""
        result = self.templates.get_help_message()

        assert "/bind" in result
        assert "/checkin" in result
        assert "/aff" in result
        assert "/help" in result

    def test_get_node_report(self):
        """测试节点播报"""
        result = self.templates.get_node_report(
            total=10,
            online=8,
            offline=2,
            node_details="节点A: 在线\n节点B: 离线",
            report_time="20:30"
        )

        assert "10" in result
        assert "8" in result
        assert "2" in result
        assert "20:30" in result
