"""
Celery Configuration

Task queue configuration with Celery.
Supports second-level precision scheduling and configurable concurrency.
"""

import os

from celery import Celery
from celery.schedules import crontab

from app.core.config import settings


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return max(1, parsed)


celery_app = Celery(
    "vanguard",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.core.scheduler.tasks",
        "app.modules.qq.tasks",
    ],
)

# =============================================================================
# Task Concurrency Configuration
# =============================================================================
TASK_CONCURRENCY = {
    "health_check": 5,
    "send_messages": 10,
    "proxy_validation": 3,
    "campaign_check": 2,
    "bulk_import": 2,
    "automation": 3,
    "qq_commands": 2,
}

# =============================================================================
# Celery Configuration
# =============================================================================
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=3600,  # 1 hour
    task_soft_time_limit=3000,  # 50 minutes
    worker_prefetch_multiplier=_env_int("CELERY_WORKER_PREFETCH_MULTIPLIER", 1),
    worker_max_tasks_per_child=_env_int("CELERY_WORKER_MAX_TASKS_PER_CHILD", 100),
    task_acks_late=True,
    task_reject_on_worker_lost=True,
)

# =============================================================================
# Beat Schedule - Second-level Precision Tasks
# =============================================================================
celery_app.conf.beat_schedule = {
    # -------------------------------------------------------------------------
    # Second-level Tasks
    # -------------------------------------------------------------------------
    "campaign-check-every-30s": {
        "task": "app.core.scheduler.tasks.campaign_check_task",
        "schedule": 30.0,  # Every 30 seconds
        "options": {"queue": "campaign_check"},
    },
    "user-state-check-every-minute": {
        "task": "app.core.scheduler.tasks.check_user_states",
        "schedule": 60.0,  # Every minute
        "options": {"queue": "default"},
    },
    # -------------------------------------------------------------------------
    # Minute-level Tasks
    # -------------------------------------------------------------------------
    "health-check-accounts-every-5min": {
        "task": "app.core.scheduler.tasks.health_check_accounts",
        "schedule": crontab(minute="*/5"),  # Every 5 minutes
        "options": {"queue": "health_check", "rate_limit": "10/m"},
    },
    "health-check-proxies-every-5min": {
        "task": "app.core.scheduler.tasks.health_check_proxies",
        "schedule": crontab(minute="*/5"),
        "options": {"queue": "health_check", "rate_limit": "10/m"},
    },
    "sync-group-metrics-every-10min": {
        "task": "app.core.scheduler.tasks.sync_group_metrics",
        "schedule": crontab(minute="*/10"),
        "options": {"queue": "default"},
    },
    "check-proxy-status-every-15min": {
        "task": "app.core.scheduler.tasks.check_proxy_status",
        "schedule": crontab(minute="*/15"),
        "options": {"queue": "proxy_validation"},
    },
    "auto-join-groups-dispatcher-every-5min": {
        "task": "app.core.scheduler.tasks.auto_join_groups_task",
        "schedule": crontab(minute="*/5"),
        "kwargs": {"scheduled": True, "keywords_per_account": 10, "max_groups_per_keyword": 20},
        "options": {"queue": "automation", "rate_limit": "12/h"},
    },
    "recover-orphaned-groups-every-5min": {
        "task": "app.core.scheduler.tasks.recover_orphaned_groups_task",
        "schedule": crontab(minute="*/5"),
        "kwargs": {"max_tasks": 20},
        "options": {"queue": "automation", "rate_limit": "12/h"},
    },
    "deliver-ads-every-10min": {
        "task": "app.core.scheduler.tasks.deliver_ads_task",
        "schedule": 600.0,
        "options": {"queue": "automation", "rate_limit": "6/h"},
    },
    "check-ad-survival-every-2min": {
        "task": "app.core.scheduler.tasks.check_ad_survival_task",
        "schedule": 120.0,
        "options": {"queue": "automation", "rate_limit": "30/h"},
    },
    "audit-group-ad-policies-hourly": {
        "task": "app.core.scheduler.tasks.audit_group_ad_policies_task",
        "schedule": crontab(minute=15),
        "kwargs": {"limit": 5},
        "options": {"queue": "automation", "rate_limit": "1/h"},
    },
    "auto-probe-unknown-ad-policies-every-5min": {
        "task": "app.core.scheduler.tasks.auto_probe_unknown_group_ad_policies_task",
        "schedule": crontab(minute="*/5"),
        "options": {"queue": "automation", "rate_limit": "12/h"},
    },
    "group-ai-warmup-dispatcher-every-30min": {
        "task": "app.core.scheduler.tasks.group_ai_warmup_task",
        "schedule": crontab(minute="*/30"),
        "options": {"queue": "automation", "rate_limit": "2/h"},
    },
    # -------------------------------------------------------------------------
    # Hour-level Tasks
    # -------------------------------------------------------------------------
    "cleanup-expired-tokens-hourly": {
        "task": "app.core.scheduler.tasks.cleanup_expired_tokens",
        "schedule": crontab(minute=0),  # Every hour at minute 0
        "options": {"queue": "default"},
    },
    "sync-group-info-hourly": {
        "task": "app.core.scheduler.tasks.sync_group_info",
        "schedule": crontab(minute=30),  # Every hour at minute 30
        "options": {"queue": "default"},
    },
    # -------------------------------------------------------------------------
    # Daily Tasks
    # -------------------------------------------------------------------------
    "cleanup-old-messages-daily": {
        "task": "app.core.scheduler.tasks.cleanup_old_messages",
        "schedule": crontab(hour=3, minute=0),  # Daily at 3:00 AM
        "options": {"queue": "default"},
    },
    "generate-daily-report-at-2am": {
        "task": "app.core.scheduler.tasks.generate_daily_report",
        "schedule": crontab(hour=2, minute=0),  # Daily at 2:00 AM
        "options": {"queue": "default"},
    },
    "maintain-account-risk-daily": {
        "task": "app.core.scheduler.tasks.maintain_account_risk",
        "schedule": crontab(hour=2, minute=30),
        "options": {"queue": "default"},
    },
    "broadcast-node-status-at-830pm": {
        "task": "app.core.scheduler.tasks.broadcast_node_status",
        "schedule": crontab(hour=20, minute=30),  # Daily at 8:30 PM
        "options": {"queue": "broadcast"},
    },
    # -------------------------------------------------------------------------
    # Trial Reminder (check every hour, send actual reminder at specific times)
    # -------------------------------------------------------------------------
    "process-pending-campaigns-every-5min": {
        "task": "app.core.scheduler.tasks.process_pending_campaigns",
        "schedule": crontab(minute="*/5"),
        "options": {"queue": "default"},
    },
    "cleanup-qq-messages-daily": {
        "task": "app.modules.qq.tasks.cleanup_qq_messages",
        "schedule": crontab(hour=3, minute=30),
        "options": {"queue": "qq_commands"},
    },
}

# =============================================================================
# Task Routing to Queues
# =============================================================================
celery_app.conf.task_routes = {
    "app.core.scheduler.tasks.health_check_*": {"queue": "health_check"},
    "app.core.scheduler.tasks.check_proxy_status": {"queue": "proxy_validation"},
    "app.core.scheduler.tasks.campaign_check_task": {"queue": "campaign_check"},
    "app.core.scheduler.tasks.send_bulk_messages": {"queue": "send_messages"},
    "app.core.scheduler.tasks.send_trial_reminder": {"queue": "send_messages"},
    "app.core.scheduler.tasks.broadcast_node_status": {"queue": "broadcast"},
    "app.core.scheduler.tasks.execute_broadcast_record": {"queue": "broadcast"},
    "app.core.scheduler.tasks.import_accounts_batch": {"queue": "bulk_import"},
    "app.core.scheduler.tasks.validate_proxy_batch": {"queue": "proxy_validation"},
    "app.core.scheduler.tasks.replenish_keywords_task": {"queue": "automation"},
    "app.core.scheduler.tasks.auto_join_groups_task": {"queue": "automation"},
    "app.core.scheduler.tasks.deliver_ads_task": {"queue": "automation"},
    "app.core.scheduler.tasks.recover_orphaned_groups_task": {"queue": "automation"},
    "app.core.scheduler.tasks.check_ad_survival_task": {"queue": "automation"},
    "app.core.scheduler.tasks.audit_group_ad_policies_task": {"queue": "automation"},
    "app.modules.qq.tasks.execute_qq_command": {"queue": "qq_commands"},
    "app.modules.qq.tasks.cleanup_qq_messages": {"queue": "qq_commands"},
}

# =============================================================================
# Queue Configuration
# =============================================================================
celery_app.conf.task_queues = {
    "default": {
        "exchange": "default",
        "routing_key": "default",
    },
    "health_check": {
        "exchange": "health_check",
        "routing_key": "health_check",
    },
    "send_messages": {
        "exchange": "send_messages",
        "routing_key": "send_messages",
    },
    "proxy_validation": {
        "exchange": "proxy_validation",
        "routing_key": "proxy_validation",
    },
    "campaign_check": {
        "exchange": "campaign_check",
        "routing_key": "campaign_check",
    },
    "bulk_import": {
        "exchange": "bulk_import",
        "routing_key": "bulk_import",
    },
    "broadcast": {
        "exchange": "broadcast",
        "routing_key": "broadcast",
    },
    "automation": {
        "exchange": "automation",
        "routing_key": "automation",
    },
    "qq_commands": {
        "exchange": "qq_commands",
        "routing_key": "qq_commands",
    },
}

# =============================================================================
# Alert Configuration
# =============================================================================
celery_app.conf.task_annotations = {
    "*": {
        "rate_limit": "100/m",
    },
}

# Alert thresholds
ALERT_THRESHOLDS = {
    "task_failure_count": 5,  # Alert after 5 consecutive failures
    "task_timeout_count": 3,  # Alert after 3 timeouts
    "worker_offline_seconds": 300,  # Alert after 5 minutes offline
}
