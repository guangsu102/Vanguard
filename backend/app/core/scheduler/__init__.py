"""
Scheduler Module Initialization

Exports scheduler components, tasks, and utilities.
"""

from app.core.scheduler.scheduler import TaskScheduler, get_scheduler
from app.core.scheduler.tasks import (
    health_check_accounts,
    health_check_proxies,
    cleanup_old_messages,
    sync_group_info,
    sync_group_metrics,
    check_user_states,
    broadcast_node_status,
    execute_broadcast_record,
    send_trial_reminder,
    process_pending_campaigns,
    replenish_keywords_task,
    auto_join_groups_task,
    recover_orphaned_groups_task,
    deliver_ads_task,
    check_ad_survival_task,
    campaign_check_task,
    send_bulk_messages,
    import_accounts_batch,
    process_user_registration,
    execute_campaign_rewards,
    validate_proxy_batch,
    check_proxy_status,
    cleanup_expired_tokens,
    generate_daily_report,
    get_task_status,
    cleanup_completed_tasks,
)
from app.core.scheduler.alerts import (
    AlertManager,
    TaskAlertManager,
    AlertSeverity,
)
from app.core.scheduler.worker import (
    start_worker,
    start_beat,
    start_flower,
    start_multi_workers,
    get_worker_status,
    shutdown_worker,
    QUEUE_CONFIGS,
)

__all__ = [
    # Scheduler
    "TaskScheduler",
    "get_scheduler",
    # Periodic Tasks
    "health_check_accounts",
    "health_check_proxies",
    "cleanup_old_messages",
    "sync_group_info",
    "sync_group_metrics",
    "check_user_states",
    "broadcast_node_status",
    "execute_broadcast_record",
    "send_trial_reminder",
    "process_pending_campaigns",
    "replenish_keywords_task",
    "auto_join_groups_task",
    "recover_orphaned_groups_task",
    "deliver_ads_task",
    "check_ad_survival_task",
    "campaign_check_task",
    "check_proxy_status",
    "cleanup_expired_tokens",
    "generate_daily_report",
    # Async Tasks
    "send_bulk_messages",
    "import_accounts_batch",
    "process_user_registration",
    "execute_campaign_rewards",
    "validate_proxy_batch",
    # Task Utilities
    "get_task_status",
    "cleanup_completed_tasks",
    # Alerts
    "AlertManager",
    "TaskAlertManager",
    "AlertSeverity",
    # Worker Management
    "start_worker",
    "start_beat",
    "start_flower",
    "start_multi_workers",
    "get_worker_status",
    "shutdown_worker",
    "QUEUE_CONFIGS",
]
