"""
Unit Tests for Scheduler Module

Tests for tasks, alerts, and worker management.
"""

import asyncio
import sys

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add backend to path
sys.path.insert(0, "d:/tanxuan/project/Vanguard/backend")


class TestAlertManager:
    """Tests for AlertManager."""

    def test_alert_severity_emoji(self):
        """Test severity emoji mapping."""
        from app.core.scheduler.alerts import AlertManager, AlertSeverity

        manager = AlertManager()

        assert manager._get_severity_emoji(AlertSeverity.INFO) == "ℹ️"
        assert manager._get_severity_emoji(AlertSeverity.WARNING) == "⚠️"
        assert manager._get_severity_emoji(AlertSeverity.ERROR) == "❌"
        assert manager._get_severity_emoji(AlertSeverity.CRITICAL) == "🔴"

    def test_alert_severity_format(self):
        """Test severity formatting."""
        from app.core.scheduler.alerts import AlertManager, AlertSeverity

        manager = AlertManager()

        assert manager._format_severity(AlertSeverity.INFO) == "INFO"
        assert manager._format_severity(AlertSeverity.WARNING) == "WARNING"
        assert manager._format_severity(AlertSeverity.ERROR) == "ERROR"
        assert manager._format_severity(AlertSeverity.CRITICAL) == "CRITICAL"

    def test_track_failure_increments(self):
        """Test failure tracking increments counter."""
        from app.core.scheduler.alerts import AlertManager

        manager = AlertManager()

        assert manager.track_failure("task1") == 1
        assert manager.track_failure("task1") == 2
        assert manager.track_failure("task1") == 3

    def test_reset_failures_clears_counter(self):
        """Test reset clears failure counter."""
        from app.core.scheduler.alerts import AlertManager

        manager = AlertManager()

        manager.track_failure("task1")
        manager.track_failure("task1")
        manager.reset_failures("task1")

        assert manager.track_failure("task1") == 1

    def test_send_task_alert_disabled(self):
        """Test alert not sent when disabled."""
        from app.core.scheduler.alerts import AlertManager

        manager = AlertManager()
        manager._enabled = False

        result = manager.send_task_alert(
            task_name="test_task",
            error="Test error",
        )

        assert result is False

    @patch("app.core.scheduler.alerts.TelegramClient")
    def test_send_task_alert_enabled(self, mock_client_class):
        """Test alert sent when enabled."""
        from app.core.scheduler.alerts import AlertManager

        mock_client = MagicMock()
        mock_client.send_message = AsyncMock()
        mock_client_class.return_value = mock_client

        manager = AlertManager()
        manager._enabled = True
        manager._alert_chat_id = "-100123456789"

        result = manager.send_task_alert(
            task_name="test_task",
            error="Test error",
        )

        assert result is True
        mock_client.send_message.assert_called_once()

    def test_send_worker_alert_format(self):
        """Test worker alert message format."""
        from app.core.scheduler.alerts import AlertManager

        manager = AlertManager()
        manager._enabled = False

        # Should not raise and return False when disabled
        result = manager.send_worker_alert(
            worker_name="worker1",
            status="online",
        )

        assert result is False

    def test_send_queue_alert_below_threshold(self):
        """Test queue alert not sent when below threshold."""
        from app.core.scheduler.alerts import AlertManager

        manager = AlertManager()
        manager._enabled = False

        result = manager.send_queue_alert(
            queue_name="default",
            length=100,
            threshold=1000,
        )

        assert result is False


class TestWorkerConfig:
    """Tests for worker configuration."""

    def test_queue_configs_defined(self):
        """Test queue configurations are defined."""
        from app.core.scheduler.worker import QUEUE_CONFIGS

        assert "default" in QUEUE_CONFIGS
        assert "health_check" in QUEUE_CONFIGS
        assert "send_messages" in QUEUE_CONFIGS
        assert "campaign_check" in QUEUE_CONFIGS

    def test_queue_configs_have_concurrency(self):
        """Test each queue has concurrency setting."""
        from app.core.scheduler.worker import QUEUE_CONFIGS

        for queue_name, config in QUEUE_CONFIGS.items():
            assert "concurrency" in config
            assert isinstance(config["concurrency"], int)
            assert config["concurrency"] > 0

    def test_get_queues_arg(self):
        """Test queues argument generation."""
        from app.core.scheduler.worker import get_queues_arg

        queues = get_queues_arg()

        assert "default" in queues
        assert "health_check" in queues
        assert "send_messages" in queues

    def test_get_concurrency_for_queue(self):
        """Test concurrency retrieval for queues."""
        from app.core.scheduler.worker import get_concurrency_for_queue

        assert get_concurrency_for_queue("health_check") == 5
        assert get_concurrency_for_queue("send_messages") == 10
        assert get_concurrency_for_queue("unknown") == 4  # default value


class TestCeleryConfig:
    """Tests for Celery configuration."""

    def test_task_concurrency_configured(self):
        """Test task concurrency settings."""
        from app.celery import TASK_CONCURRENCY

        assert "health_check" in TASK_CONCURRENCY
        assert "send_messages" in TASK_CONCURRENCY
        assert TASK_CONCURRENCY["health_check"] == 5

    def test_beat_schedule_has_second_level_tasks(self):
        """Test beat schedule includes second-level precision tasks."""
        from app.celery import celery_app

        beat_schedule = celery_app.conf.beat_schedule

        # Check for 30-second task
        assert "campaign-check-every-30s" in beat_schedule
        assert beat_schedule["campaign-check-every-30s"]["schedule"] == 30.0
        assert "auto-join-groups-dispatcher-every-5min" in beat_schedule
        auto_join_schedule = beat_schedule["auto-join-groups-dispatcher-every-5min"]
        assert str(auto_join_schedule["schedule"]) == "<crontab: */5 * * * * (m/h/dM/MY/d)>"
        assert auto_join_schedule["kwargs"] == {
            "scheduled": True,
            "keywords_per_account": 10,
            "max_groups_per_keyword": 20,
        }
        deliver_ads_schedule = beat_schedule["deliver-ads-every-10min"]
        assert deliver_ads_schedule["schedule"] == 600.0
        assert deliver_ads_schedule["options"]["rate_limit"] == "6/h"
        assert beat_schedule["check-ad-survival-every-2min"]["schedule"] == 120.0
        assert "group-ai-warmup-dispatcher-every-30min" in beat_schedule
        ad_policy_audit_schedule = beat_schedule["audit-group-ad-policies-hourly"]
        assert ad_policy_audit_schedule["options"]["rate_limit"] == "1/h"

    def test_ad_delivery_runtime_interval_is_not_shortened(self):
        """Test the runtime guard honors the configured delivery interval."""
        from app.core import automation_settings, database
        from app.core.scheduler import tasks

        db_context = MagicMock()
        db_context.__aenter__ = AsyncMock(return_value=MagicMock())
        db_context.__aexit__ = AsyncMock(return_value=None)
        redis_client = MagicMock()
        redis_client.get = AsyncMock(return_value="880")
        redis_client.set = AsyncMock()
        redis_client.aclose = AsyncMock()

        with (
            patch.object(tasks, "_ensure_db_initialized", new=AsyncMock()),
            patch.object(database, "get_db_session", return_value=db_context),
            patch.object(
                automation_settings,
                "get_ad_delivery_execution_settings",
                new=AsyncMock(
                    return_value={
                        "enabled": True,
                        "dispatcher_interval_seconds": 600,
                        "group_campaign_cooldown_minutes": 4320,
                        "stop_account_after_success": False,
                        "stop_account_after_failure": False,
                    }
                ),
            ),
            patch.object(tasks, "_new_scheduler_redis_client", new=AsyncMock(return_value=redis_client)),
            patch.object(tasks.time, "time", return_value=1000.0),
        ):
            reservation = asyncio.run(tasks._reserve_ad_delivery_execution())

        assert reservation["should_run"] is False
        assert reservation["reason"] == "ad_delivery_interval"
        assert reservation["next_run_at"] == "1970-01-01T00:24:40"
        redis_client.set.assert_not_awaited()

    def test_finish_execution_records_reservation_start_time(self):
        from app.core.scheduler import tasks

        redis_client = MagicMock()
        redis_client.set = AsyncMock()
        redis_client.delete = AsyncMock()
        redis_client.aclose = AsyncMock()

        with patch.object(tasks, "_new_scheduler_redis_client", new=AsyncMock(return_value=redis_client)):
            asyncio.run(tasks._finish_ad_delivery_execution(1234.5))

        redis_client.set.assert_awaited_once_with(tasks.AD_DELIVERY_LAST_RUN_KEY, "1234.5")
        redis_client.delete.assert_awaited_once_with(tasks.AD_DELIVERY_LOCK_KEY)

    def test_beat_schedule_has_minute_level_tasks(self):
        """Test beat schedule includes minute-level tasks."""
        from app.celery import celery_app

        beat_schedule = celery_app.conf.beat_schedule

        # Check for 5-minute tasks
        assert "health-check-accounts-every-5min" in beat_schedule

    def test_beat_schedule_has_hourly_tasks(self):
        """Test beat schedule includes hourly tasks."""
        from app.celery import celery_app

        beat_schedule = celery_app.conf.beat_schedule

        assert "cleanup-expired-tokens-hourly" in beat_schedule

    def test_beat_schedule_has_daily_tasks(self):
        """Test beat schedule includes daily tasks."""
        from app.celery import celery_app

        beat_schedule = celery_app.conf.beat_schedule

        assert "generate-daily-report-at-2am" in beat_schedule

    def test_task_routes_configured(self):
        """Test task routing is configured."""
        from app.celery import celery_app

        routes = celery_app.conf.task_routes

        assert "app.core.scheduler.tasks.health_check_*" in routes
        assert "app.core.scheduler.tasks.send_bulk_messages" in routes

    def test_alert_thresholds_configured(self):
        """Test alert thresholds are defined."""
        from app.celery import ALERT_THRESHOLDS

        assert "task_failure_count" in ALERT_THRESHOLDS
        assert "worker_offline_seconds" in ALERT_THRESHOLDS


class TestTaskDefinitions:
    """Tests for task definitions."""

    def test_run_async_initializes_and_closes_loop_bound_resources(self):
        from app.core import database, redis
        from app.core.scheduler import tasks

        events = []

        async def init_redis():
            events.append("redis_init")

        async def operation():
            events.append("operation")
            return "completed"

        async def close_redis():
            events.append("redis_close")

        async def close_db():
            events.append("db_close")

        with (
            patch.object(redis, "redis_client", MagicMock()),
            patch.object(redis, "init_redis", new=init_redis),
            patch.object(redis, "close_redis", new=close_redis),
            patch.object(database, "engine", MagicMock()),
            patch.object(database, "close_db", new=close_db),
        ):
            result = tasks._run_async(operation())

        assert result == "completed"
        assert events == ["redis_init", "operation", "redis_close", "db_close"]

    def test_scheduler_interval_tolerates_small_dispatch_jitter(self):
        from app.core.scheduler.tasks import _scheduler_interval_is_due

        assert _scheduler_interval_is_due(1000.0, 1589.9, 600) is False
        assert _scheduler_interval_is_due(1000.0, 1590.0, 600) is True
        assert _scheduler_interval_is_due(1000.0, 1599.999, 600) is True
        assert _scheduler_interval_is_due(None, 1000.0, 600) is True

    def test_health_check_accounts_task_exists(self):
        """Test health_check_accounts task is defined."""
        from app.core.scheduler.tasks import health_check_accounts

        assert health_check_accounts is not None
        assert hasattr(health_check_accounts, "apply_async")

    def test_campaign_check_task_exists(self):
        """Test campaign_check_task exists for second-level scheduling."""
        from app.core.scheduler.tasks import campaign_check_task

        assert campaign_check_task is not None
        assert hasattr(campaign_check_task, "apply_async")

    def test_send_bulk_messages_task_exists(self):
        """Test send_bulk_messages task is defined."""
        from app.core.scheduler.tasks import send_bulk_messages

        assert send_bulk_messages is not None
        assert hasattr(send_bulk_messages, "apply_async")

    def test_import_accounts_batch_task_exists(self):
        """Test import_accounts_batch task is defined."""
        from app.core.scheduler.tasks import import_accounts_batch

        assert import_accounts_batch is not None
        assert hasattr(import_accounts_batch, "apply_async")

    def test_get_task_status_task_exists(self):
        """Test get_task_status utility task exists."""
        from app.core.scheduler.tasks import get_task_status

        assert get_task_status is not None

    def test_result_skip_reasons_counts_only_skip_details(self):
        from app.core.scheduler.tasks import _result_skip_reasons

        result = {
            "details": [
                {"action": "skip", "reason": "daily_join_quota"},
                {"reason": "daily_join_quota"},
                {"action": "dry_run", "reason": "dry_run"},
                {"action": "stop_after_failure", "reason": "account_banned"},
                {"action": "skip"},
                "invalid",
            ]
        }

        assert _result_skip_reasons(result) == {
            "daily_join_quota": 2,
            "dry_run": 1,
        }


class TestSchedulerModuleExports:
    """Tests for module exports."""

    def test_scheduler_exports_tasks(self):
        """Test __init__.py exports tasks."""
        from app.core.scheduler import (
            health_check_accounts,
            send_bulk_messages,
            campaign_check_task,
        )

        assert health_check_accounts is not None
        assert send_bulk_messages is not None
        assert campaign_check_task is not None

    def test_scheduler_exports_alerts(self):
        """Test __init__.py exports alert components."""
        from app.core.scheduler import (
            AlertManager,
            TaskAlertManager,
            AlertSeverity,
        )

        assert AlertManager is not None
        assert TaskAlertManager is not None
        assert AlertSeverity is not None

    def test_scheduler_exports_worker(self):
        """Test __init__.py exports worker functions."""
        from app.core.scheduler import (
            start_worker,
            start_beat,
            start_flower,
            get_worker_status,
            QUEUE_CONFIGS,
        )

        assert start_worker is not None
        assert start_beat is not None
        assert start_flower is not None
        assert get_worker_status is not None
        assert QUEUE_CONFIGS is not None

    def test_all_tasks_in_exports(self):
        """Test all tasks are in __all__."""
        import app.core.scheduler as scheduler

        expected_tasks = [
            "health_check_accounts",
            "health_check_proxies",
            "cleanup_old_messages",
            "sync_group_info",
            "sync_group_metrics",
            "check_user_states",
            "broadcast_node_status",
            "send_trial_reminder",
            "process_pending_campaigns",
            "campaign_check_task",
            "send_bulk_messages",
            "import_accounts_batch",
            "process_user_registration",
            "execute_campaign_rewards",
            "validate_proxy_batch",
        ]

        for task_name in expected_tasks:
            assert hasattr(scheduler, task_name), f"Missing task: {task_name}"


class TestTaskRetryConfig:
    """Tests for task retry configuration."""

    def test_health_check_has_retry(self):
        """Test health_check_accounts has retry configured."""
        from app.core.scheduler.tasks import health_check_accounts

        assert health_check_accounts.max_retries == 3

    def test_campaign_check_has_retry(self):
        """Test campaign_check_task has retry configured."""
        from app.core.scheduler.tasks import campaign_check_task

        assert campaign_check_task.max_retries == 5
        assert campaign_check_task.default_retry_delay == 10

    def test_send_bulk_messages_has_retry(self):
        """Test send_bulk_messages has retry configured."""
        from app.core.scheduler.tasks import send_bulk_messages

        assert send_bulk_messages.max_retries == 3
        assert send_bulk_messages.default_retry_delay == 60


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
