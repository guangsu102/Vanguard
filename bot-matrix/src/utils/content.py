"""内容文案模板库"""
import random
from typing import Optional


class ContentTemplates:
    """营销文案模板"""

    # 签到成功文案
    CHECKIN_TEMPLATES = [
        {
            "title": "签到成功！",
            "messages": [
                "{name}，今日签到获得 <b>{bonus}MB</b> 流量！",
                "运气不错，{name}！签到获得 <b>{bonus}MB</b>，已存入您的账户~",
                "🎉 {name} 签到成功！获得 <b>{bonus}MB</b> 流量奖励！",
            ],
            "suffix": "坚持签到，流量多多！"
        },
        {
            "title": "签到成功",
            "messages": [
                "Hey {name}，又来签到啦？不错不错！今日获得 <b>{bonus}MB</b>~",
                "{name}，签到是一种习惯！今日收益 <b>{bonus}MB</b>，已到账！",
            ],
            "suffix": "明天再来，更有好礼相送！"
        }
    ]

    # 特殊奖励文案
    SPECIAL_CHECKIN_TEMPLATES = [
        "🎊 <b>双倍惊喜！</b> {name} 触发幸运buff，获得 {bonus}MB 双倍流量！",
        "🌟 <b>欧皇降临！</b> {name} 鸿运当头，签到奖励翻倍！{bonus}MB 已到账！",
        "💫 <b>天选之人！</b> {name}，你的运气爆棚了！{bonus}MB 流量double！",
    ]

    # 弃单挽回文案
    ABANDONED_CART_TEMPLATES = [
        {
            "main": "检测到您有一笔未完成的订单",
            "body": "订单号：{order_id}\n金额：{amount}\n\n别让优惠悄悄溜走~",
            "action": "限时 {validity_hours} 小时使用此 {discount}% 折扣码完成支付",
            "code_label": "您的专属折扣码："
        },
        {
            "main": "您好，还记得这笔订单吗？",
            "body": "订单 {order_id} 还未支付\n我们为您准备了专属优惠",
            "action": "使用折扣码立省 {discount}%，有效期仅剩 {validity_hours} 小时！",
            "code_label": "折扣码："
        }
    ]

    # 节点状态播报文案
    NODE_REPORT_TEMPLATE = """📊 节点状态播报

总节点数：{total}
在线：{online} ✅
离线：{offline} ❌

详细节点状态：
{node_details}

━━━━━━━━━━━━━━━━━━
报告时间：{report_time}
XBoard - 高速稳定，值得信赖"""

    # 欢迎消息
    WELCOME_MESSAGE = """👋 欢迎加入 XBoard！

感谢您的关注！
请先完成以下订阅任务获取试用资格。
"""

    # 帮助消息
    HELP_MESSAGE = """📖 使用帮助

/bind <密钥> - 绑定 XBoard 账号
/checkin - 每日签到领流量
/aff - 生成推广海报
/myinfo - 查看账号信息
/help - 显示此帮助

如有疑问请联系管理员"""

    def get_checkin_success(
        self,
        name: str,
        bonus_mb: int,
        is_special: bool = False
    ) -> str:
        """获取签到成功文案"""
        if is_special:
            template = random.choice(self.SPECIAL_CHECKIN_TEMPLATES)
            return template.format(name=name, bonus=bonus_mb)

        template_group = random.choice(self.CHECKIN_TEMPLATES)
        message = random.choice(template_group["messages"])
        return message.format(name=name, bonus=bonus_mb) + f"\n\n{template_group['suffix']}"

    def get_abandoned_cart_message(
        self,
        order_id: str,
        amount: str,
        coupon_code: str,
        discount: int,
        validity_hours: int
    ) -> str:
        """获取弃单挽回文案"""
        template = random.choice(self.ABANDONED_CART_TEMPLATES)

        message = (
            f"📢 {template['main']}\n\n"
            f"{template['body']}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{template['action']}\n\n"
            f"{template['code_label']}\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"<code>{coupon_code}</code>\n\n"
            f"立即支付 →"
        )

        return message.format(
            order_id=order_id,
            amount=amount,
            discount=discount,
            validity_hours=validity_hours
        )

    def get_node_report(
        self,
        total: int,
        online: int,
        offline: int,
        node_details: str,
        report_time: str
    ) -> str:
        """获取节点播报文案"""
        return self.NODE_REPORT_TEMPLATE.format(
            total=total,
            online=online,
            offline=offline,
            node_details=node_details,
            report_time=report_time
        )

    def get_welcome_message(self) -> str:
        """获取欢迎消息"""
        return self.WELCOME_MESSAGE

    def get_help_message(self) -> str:
        """获取帮助消息"""
        return self.HELP_MESSAGE
