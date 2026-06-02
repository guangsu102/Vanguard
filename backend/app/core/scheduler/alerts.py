"""
Task Alert Manager

Telegram-based alerting for task failures and monitoring.
"""

import asyncio
from datetime import datetime
from enum import Enum
from typing import Optional

import structlog

from app.core.config import settings


logger = structlog.get_logger()


class AlertSeverity(str, Enum):
    """Alert severity levels."""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class AlertManager:
    """
    Manages alerts for task failures and monitoring events.

    Sends alerts to configured Telegram chat (alert group).
    """

    def __init__(self):
        """Initialize alert manager."""
        self._alert_chat_id = getattr(settings, "ALERT_CHAT_ID", None)
        self._enabled = self._alert_chat_id is not None
        self._failure_count = {}  # Track consecutive failures

    @property
    def is_enabled(self) -> bool:
        """Check if alerting is enabled."""
        return self._enabled

    def _get_severity_emoji(self, severity: AlertSeverity) -> str:
        """Get emoji for severity level."""
        emojis = {
            AlertSeverity.INFO: "ℹ️",
            AlertSeverity.WARNING: "⚠️",
            AlertSeverity.ERROR: "❌",
            AlertSeverity.CRITICAL: "🔴",
        }
        return emojis.get(severity, "ℹ️")

    def _format_severity(self, severity: AlertSeverity) -> str:
        """Format severity for display."""
        return severity.value.upper()

    def _send_telegram_message(self, message: str) -> bool:
        """
        Send message to Telegram alert chat.

        Args:
            message: Message content (Markdown supported)

        Returns:
            True if sent successfully
        """
        if not self._enabled:
            logger.debug("alerts_disabled_skip_telegram_send")
            return False

        try:
            if not settings.BOT_TOKEN:
                logger.debug("alert_bot_token_missing")
                return False

            from app.integrations.telegram.client import TelegramClient, TelegramConfig

            async def _send() -> None:
                client = TelegramClient(TelegramConfig(bot_token=settings.BOT_TOKEN))
                try:
                    await client.send_message(self._alert_chat_id, message, parse_mode="Markdown")
                finally:
                    await client.close()

            asyncio.run(_send())
            logger.info("alert_sent_to_telegram", chat_id=self._alert_chat_id)
            return True

        except Exception as e:
            logger.error("failed_to_send_telegram_alert", error=str(e))
            return False

    def send_task_alert(
        self,
        task_name: str,
        error: str,
        severity: AlertSeverity = AlertSeverity.ERROR,
        details: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> bool:
        """
        Send task failure alert.

        Args:
            task_name: Name of the failed task
            error: Error message
            severity: Alert severity level
            details: Additional details
            task_id: Celery task ID if applicable

        Returns:
            True if alert was sent
        """
        emoji = self._get_severity_emoji(severity)
        severity_text = self._format_severity(severity)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        message_parts = [
            f"{emoji} *任务执行{severity_text}*",
            "",
            f"📋 *任务名:* `{task_name}`",
            f"❌ *错误:* `{error[:200]}`",
            f"⏰ *时间:* `{timestamp}`",
        ]

        if task_id:
            message_parts.append(f"🔧 *Task ID:* `{task_id}`")

        if details:
            message_parts.extend(["", f"📝 *详情:* {details}"])

        message = "\n".join(message_parts)

        logger.info(
            "sending_task_alert",
            task_name=task_name,
            severity=severity.value,
        )

        return self._send_telegram_message(message)

    def send_worker_alert(
        self,
        worker_name: str,
        status: str,
        details: Optional[str] = None,
    ) -> bool:
        """
        Send worker status alert.

        Args:
            worker_name: Worker name
            status: Status (online/offline/restarting)
            details: Additional details

        Returns:
            True if alert was sent
        """
        status_emoji = {
            "online": "🟢",
            "offline": "🔴",
            "restarting": "🟡",
        }.get(status, "⚪")

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        message_parts = [
            f"{status_emoji} *Worker 状态变更*",
            "",
            f"🖥️ *Worker:* `{worker_name}`",
            f"📊 *状态:* `{status.upper()}`",
            f"⏰ *时间:* `{timestamp}`",
        ]

        if details:
            message_parts.extend(["", f"📝 *详情:* {details}"])

        message = "\n".join(message_parts)

        logger.info("sending_worker_alert", worker_name=worker_name, status=status)
        return self._send_telegram_message(message)

    def send_queue_alert(
        self,
        queue_name: str,
        length: int,
        threshold: int = 1000,
    ) -> bool:
        """
        Send queue backlog alert.

        Args:
            queue_name: Queue name
            length: Current queue length
            threshold: Alert threshold

        Returns:
            True if alert was sent
        """
        if length < threshold:
            return False

        emoji = "⚠️" if length < threshold * 2 else "🔴"
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        message = f"""{emoji} *队列积压告警*

📋 *队列:* `{queue_name}`
📊 *当前长度:* `{length}`
⚠️ *阈值:* `{threshold}`
⏰ *时间:* `{timestamp}`
"""

        logger.warning("queue_backlog_alert", queue=queue_name, length=length)
        return self._send_telegram_message(message)

    def send_daily_summary(self, stats: dict) -> bool:
        """
        Send daily task execution summary.

        Args:
            stats: Dictionary with task statistics

        Returns:
            True if sent
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        completed = stats.get("completed", 0)
        failed = stats.get("failed", 0)
        success_rate = (completed / (completed + failed) * 100) if (completed + failed) > 0 else 0

        message = f"""📊 *每日任务汇总*

✅ *完成:* `{completed}`
❌ *失败:* `{failed}`
📈 *成功率:* `{success_rate:.1f}%`
⏰ *时间:* `{timestamp}`
"""

        logger.info("sending_daily_summary", stats=stats)
        return self._send_telegram_message(message)

    def track_failure(self, task_name: str) -> int:
        """
        Track consecutive task failures.

        Args:
            task_name: Task name

        Returns:
            Current failure count
        """
        count = self._failure_count.get(task_name, 0) + 1
        self._failure_count[task_name] = count
        return count

    def reset_failures(self, task_name: str) -> None:
        """
        Reset failure counter for a task.

        Args:
            task_name: Task name
        """
        self._failure_count.pop(task_name, None)


# Singleton instance
_alert_manager: Optional[AlertManager] = None


def TaskAlertManager() -> AlertManager:
    """Get singleton alert manager instance."""
    global _alert_manager
    if _alert_manager is None:
        _alert_manager = AlertManager()
    return _alert_manager
