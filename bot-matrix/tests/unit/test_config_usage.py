from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bots.lead_gen import LeadGenBot
from src.main import BotMatrix


def test_lead_gen_reads_anti_ban_from_its_yaml_section():
    bot = LeadGenBot(
        account_manager=MagicMock(),
        db=MagicMock(),
        redis=MagicMock(),
        api=MagicMock(),
        config={
            "lead_gen": {
                "anti_ban": {
                    "message_interval": 45,
                    "max_messages_per_day": 12,
                    "max_groups_per_day": 4,
                    "typing_delay": [1000, 3000],
                    "random_timing": False,
                }
            }
        },
    )

    assert bot.lead_config.message_interval == 45
    assert bot.lead_config.max_messages_per_day == 12
    assert bot.lead_config.max_groups_per_day == 4
    assert bot.lead_config.typing_delay == (1000, 3000)
    assert bot.lead_config.random_timing is False


@pytest.mark.asyncio
async def test_node_report_uses_configured_schedule_and_target_once_per_day():
    send_node_report = AsyncMock()
    matrix = BotMatrix.__new__(BotMatrix)
    matrix.config = {
        "group_ops": {
            "node_report": {
                "enabled": True,
                "schedule": datetime.now().strftime("%H:%M"),
            }
        },
        "monitoring": {"node_report_chat_id": 654321},
    }
    matrix.group_ops_bot = SimpleNamespace(send_node_report=send_node_report)
    matrix._last_node_report_date = None

    await matrix._check_node_report()
    await matrix._check_node_report()

    send_node_report.assert_awaited_once_with(654321)
