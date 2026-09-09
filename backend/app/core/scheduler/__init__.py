"""
Scheduler Module Initialization

Exports scheduler components, tasks, and utilities.
"""

from importlib import import_module

from app.core.scheduler.alerts import (
    AlertManager,
    AlertSeverity,
    TaskAlertManager,
)
from app.core.scheduler.scheduler import TaskScheduler, get_scheduler
from app.core.scheduler.tasks import (
    auto_join_groups_task,
    auto_probe_unknown_group_ad_policies_task,
    broadcast_node_status,
    campaign_check_task,
    check_ad_survival_task,
    check_proxy_status,
    check_user_states,
    cleanup_completed_tasks,
    cleanup_expired_tokens,
    cleanup_old_messages,
    deliver_ads_task,
    execute_broadcast_record,
    execute_campaign_rewards,
    generate_daily_report,
    get_task_status,
    health_check_accounts,
    health_check_proxies,
    import_accounts_batch,
    process_pending_campaigns,
    process_user_registration,
    reconcile_stale_worker_statuses,
    recover_orphaned_groups_task,
    replenish_keywords_task,
    send_bulk_messages,
    send_trial_reminder,
    sync_group_info,
    sync_group_metrics,
    validate_proxy_batch,
)

_WORKER_EXPORTS = {
    "start_worker",
    "start_beat",
    "start_flower",
    "start_multi_workers",
    "get_worker_status",
    "shutdown_worker",
    "QUEUE_CONFIGS",
}


def __getattr__(name: str):
    if name not in _WORKER_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    worker_module = import_module("app.core.scheduler.worker")
    value = getattr(worker_module, name)
    globals()[name] = value
    return value

__all__ = [
    # Scheduler
    "TaskScheduler",
    "get_scheduler",
    # Periodic Tasks
    "health_check_accounts",
    "health_check_proxies",
    "reconcile_stale_worker_statuses",
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
    "auto_probe_unknown_group_ad_policies_task",
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
