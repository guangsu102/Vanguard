"""
Celery Worker Management

Worker management with multi-queue support and configurable concurrency.
"""

import os
import signal
import sys
from typing import Optional

import structlog


logger = structlog.get_logger()


# Queue configurations based on TASK_CONCURRENCY from celery.py
QUEUE_CONFIGS = {
    "default": {"concurrency": 4, "prefetch_multiplier": 4},
    "health_check": {"concurrency": 5, "prefetch_multiplier": 2},
    "send_messages": {"concurrency": 10, "prefetch_multiplier": 1},
    "proxy_validation": {"concurrency": 3, "prefetch_multiplier": 2},
    "campaign_check": {"concurrency": 2, "prefetch_multiplier": 1},
    "bulk_import": {"concurrency": 2, "prefetch_multiplier": 1},
    "broadcast": {"concurrency": 3, "prefetch_multiplier": 1},
    "automation": {"concurrency": 3, "prefetch_multiplier": 1},
}


def get_queues_arg() -> str:
    """
    Get the -Q argument for celery worker command.

    Returns:
        Comma-separated list of queues
    """
    queues = list(QUEUE_CONFIGS.keys())
    return ",".join(queues)


def get_concurrency_for_queue(queue_name: str) -> int:
    """
    Get concurrency setting for a specific queue.

    Args:
        queue_name: Queue name

    Returns:
        Concurrency count
    """
    return QUEUE_CONFIGS.get(queue_name, {}).get("concurrency", 4)


def start_worker(
    queues: Optional[list[str]] = None,
    hostname: Optional[str] = None,
    loglevel: str = "info",
) -> None:
    """
    Start a Celery worker with specified queues.

    Args:
        queues: List of queues to consume (None = all queues)
        hostname: Custom hostname for this worker
        loglevel: Logging level
    """
    from app.celery import celery_app

    queues_arg = ",".join(queues) if queues else get_queues_arg()
    concurrency = 20  # Total concurrency across all queues

    cmd_parts = [
        "celery",
        "-A", "app.celery",
        "worker",
        f"--loglevel={loglevel}",
        f"-Q {queues_arg}",
        f"--concurrency={concurrency}",
        "--max-tasks-per-child=1000",
        "--task-acks-late=True",
        "--prefetch-multiplier=4",
    ]

    if hostname:
        cmd_parts.append(f"--hostname={hostname}")

    cmd = " ".join(cmd_parts)
    logger.info("starting_celery_worker", queues=queues_arg, command=cmd)

    os.system(cmd)


def start_multi_workers() -> None:
    """
    Start multiple workers for different queues.
    Useful for production with separate processes per queue type.
    """
    import subprocess

    workers = [
        ("worker-health", ["health_check", "campaign_check"], "health-worker@%h"),
        ("worker-messages", ["send_messages", "broadcast"], "msg-worker@%h"),
        ("worker-default", ["default", "bulk_import", "proxy_validation"], "default-worker@%h"),
        ("worker-automation", ["automation"], "automation-worker@%h"),
    ]

    processes = []

    for name, queues, hostname in workers:
        queues_arg = ",".join(queues)
        concurrency = sum(QUEUE_CONFIGS[q]["concurrency"] for q in queues)

        cmd = [
            "celery",
            "-A", "app.celery",
            "worker",
            "--loglevel=info",
            f"-Q {queues_arg}",
            f"--concurrency={concurrency}",
            f"--hostname={hostname}",
            "--max-tasks-per-child=1000",
        ]

        logger.info(f"starting_{name}", queues=queues, concurrency=concurrency)
        proc = subprocess.Popen(cmd)
        processes.append((name, proc))

    logger.info("all_workers_started", count=len(processes))

    def signal_handler(signum, frame):
        logger.info("shutdown_signal_received")
        for name, proc in processes:
            logger.info(f"stopping_{name}")
            proc.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    for name, proc in processes:
        proc.wait()


def start_beat(loglevel: str = "info") -> None:
    """
    Start Celery Beat scheduler.

    Args:
        loglevel: Logging level
    """
    cmd = f"celery -A app.celery beat --loglevel={loglevel}"
    logger.info("starting_celery_beat")
    os.system(cmd)


def start_flower(broker_url: Optional[str] = None, port: int = 5555) -> None:
    """
    Start Flower monitoring server.

    Args:
        broker_url: Redis broker URL (None = use config)
        port: Port to listen on
    """
    from app.core.config import settings

    broker = broker_url or settings.CELERY_BROKER_URL

    cmd = f"celery -A app.celery flower --broker={broker} --port={port}"
    logger.info("starting_flower", port=port, broker=broker)
    os.system(cmd)


def get_worker_status() -> dict:
    """
    Get status of all workers.

    Returns:
        Dict with worker status information
    """
    from app.celery import celery_app

    inspect = celery_app.control.inspect()

    active_tasks = inspect.active()
    registered_tasks = inspect.registered()
    stats = inspect.stats()

    return {
        "active_tasks": active_tasks,
        "registered_tasks": registered_tasks,
        "stats": stats,
    }


def shutdown_worker(hostname: Optional[str] = None) -> bool:
    """
    Shutdown a worker gracefully.

    Args:
        hostname: Worker hostname (None = all workers)

    Returns:
        True if shutdown command sent
    """
    from app.celery import celery_app

    try:
        if hostname:
            celery_app.control.broadcast("shutdown", destination=[hostname])
        else:
            celery_app.control.broadcast("shutdown")
        logger.info("shutdown_command_sent", hostname=hostname or "all")
        return True
    except Exception as e:
        logger.error("shutdown_failed", error=str(e))
        return False


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Vanguard Celery Worker Management")
    parser.add_argument(
        "command",
        choices=["worker", "beat", "flower", "multi", "status", "shutdown"],
        help="Command to run",
    )
    parser.add_argument("-Q", "--queues", nargs="+", help="Queues for worker")
    parser.add_argument("-H", "--hostname", help="Worker hostname")
    parser.add_argument("--loglevel", default="info", choices=["debug", "info", "warning", "error"])
    parser.add_argument("--port", type=int, default=5555, help="Port for flower")

    args = parser.parse_args()

    if args.command == "worker":
        start_worker(queues=args.queues, hostname=args.hostname, loglevel=args.loglevel)
    elif args.command == "beat":
        start_beat(loglevel=args.loglevel)
    elif args.command == "flower":
        start_flower(port=args.port)
    elif args.command == "multi":
        start_multi_workers()
    elif args.command == "status":
        import json
        status = get_worker_status()
        print(json.dumps(status, indent=2, default=str))
    elif args.command == "shutdown":
        shutdown_worker(hostname=args.hostname)
