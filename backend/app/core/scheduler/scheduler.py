"""
Scheduler Module

Task scheduling with APScheduler and Celery integration.

Features:
- Task registration and scheduling
- Multiple scheduler support
- Task execution management
"""

import asyncio
from datetime import datetime, timedelta
from typing import Callable, Optional

import structlog

logger = structlog.get_logger()


class TaskScheduler:
    """
    Task scheduler for managing periodic and delayed tasks.

    Provides a unified interface for task scheduling with support for
    both APScheduler (synchronous) and Celery (async/distributed).
    """

    def __init__(self):
        """Initialize TaskScheduler."""
        self._tasks: dict[str, dict] = {}
        self._running = False
        self.logger = logger.bind(module="task_scheduler")

    def register_task(
        self,
        task_id: str,
        func: Callable,
        interval_seconds: Optional[int] = None,
        cron: Optional[dict] = None,
        args: tuple = (),
        kwargs: dict = None,
    ) -> None:
        """
        Register a task for scheduled execution.

        Args:
            task_id: Unique task identifier
            func: Function to execute
            interval_seconds: Run every N seconds
            cron: Cron-style schedule dict with keys: hour, minute, second, day_of_week
            args: Positional arguments for function
            kwargs: Keyword arguments for function
        """
        task_info = {
            "func": func,
            "interval_seconds": interval_seconds,
            "cron": cron,
            "args": args or (),
            "kwargs": kwargs or {},
            "last_run": None,
            "next_run": None,
        }

        self._tasks[task_id] = task_info

        if interval_seconds:
            task_info["next_run"] = datetime.utcnow() + timedelta(seconds=interval_seconds)
        elif cron:
            task_info["next_run"] = self._calculate_next_cron_run(cron)

        self.logger.info(
            "task_registered",
            task_id=task_id,
            interval=interval_seconds,
            cron=cron,
        )

    def unregister_task(self, task_id: str) -> bool:
        """
        Unregister a task.

        Args:
            task_id: Task identifier

        Returns:
            True if task was found and removed
        """
        if task_id in self._tasks:
            del self._tasks[task_id]
            self.logger.info("task_unregistered", task_id=task_id)
            return True
        return False

    def get_task(self, task_id: str) -> Optional[dict]:
        """
        Get task information.

        Args:
            task_id: Task identifier

        Returns:
            Task info dict or None
        """
        return self._tasks.get(task_id)

    def list_tasks(self) -> dict:
        """
        List all registered tasks.

        Returns:
            Dict mapping task IDs to task info
        """
        return {
            task_id: {
                "interval_seconds": task["interval_seconds"],
                "cron": task["cron"],
                "last_run": task["last_run"].isoformat() if task["last_run"] else None,
                "next_run": task["next_run"].isoformat() if task["next_run"] else None,
            }
            for task_id, task in self._tasks.items()
        }

    async def execute_task(self, task_id: str) -> bool:
        """
        Execute a task immediately.

        Args:
            task_id: Task identifier

        Returns:
            True if task executed successfully
        """
        task = self._tasks.get(task_id)
        if not task:
            self.logger.warning("task_not_found", task_id=task_id)
            return False

        try:
            func = task["func"]
            args = task["args"]
            kwargs = task["kwargs"]

            if asyncio.iscoroutinefunction(func):
                await func(*args, **kwargs)
            else:
                func(*args, **kwargs)

            task["last_run"] = datetime.utcnow()

            if task["interval_seconds"]:
                task["next_run"] = datetime.utcnow() + timedelta(
                    seconds=task["interval_seconds"]
                )
            elif task["cron"]:
                task["next_run"] = self._calculate_next_cron_run(task["cron"])

            self.logger.info("task_executed", task_id=task_id)
            return True

        except Exception as e:
            self.logger.error(
                "task_execution_failed",
                task_id=task_id,
                error=str(e),
            )
            return False

    def _calculate_next_cron_run(self, cron: dict) -> datetime:
        """
        Calculate next run time based on cron config.

        Supports: second, minute, hour, day_of_week
        """

        now = datetime.utcnow()
        next_run = now.replace(microsecond=0)

        # Start from next minute if no specific second
        if "second" not in cron and "minute" not in cron and "hour" not in cron:
            return next_run + timedelta(minutes=1)

        # Calculate based on second
        target_second = cron.get("second", 0)
        if target_second < next_run.second:
            # Move to next minute/hour as needed
            next_run = next_run + timedelta(minutes=1)
        next_run = next_run.replace(second=target_second)

        # Apply minute constraint
        if "minute" in cron:
            target_minute = cron["minute"]
            if isinstance(target_minute, int):
                if next_run.minute > target_minute:
                    # Move to next hour
                    next_run = next_run + timedelta(hours=1)
                next_run = next_run.replace(minute=target_minute, second=target_second)
            elif isinstance(target_minute, list):
                # Find next valid minute
                found = False
                for _ in range(60):
                    if next_run.minute in target_minute:
                        found = True
                        break
                    next_run = next_run + timedelta(minutes=1)
                    if next_run.hour != (next_run - timedelta(minutes=1)).hour:
                        # Crossed hour boundary, reset seconds
                        next_run = next_run.replace(second=target_second)
                if not found:
                    next_run = next_run + timedelta(hours=1)
                    next_run = next_run.replace(minute=min(target_minute), second=target_second)

        # Apply hour constraint
        if "hour" in cron:
            target_hour = cron["hour"]
            if isinstance(target_hour, int):
                if next_run.hour > target_hour:
                    # Move to next day
                    next_run = next_run + timedelta(days=1)
                next_run = next_run.replace(hour=target_hour, minute=cron.get("minute", 0), second=target_second)
            elif isinstance(target_hour, list):
                # Find next valid hour
                found = False
                for _ in range(24):
                    if next_run.hour in target_hour:
                        found = True
                        break
                    next_run = next_run + timedelta(hours=1)
                if not found:
                    next_run = next_run + timedelta(days=1)
                    next_run = next_run.replace(hour=min(target_hour), minute=cron.get("minute", 0), second=target_second)

        # Apply day_of_week constraint
        if "day_of_week" in cron:
            target_days = cron["day_of_week"]
            if isinstance(target_days, int):
                target_days = [target_days]

            current_day = next_run.weekday()
            days_ahead = 0
            for i in range(7):
                check_day = (current_day + i) % 7
                if check_day in target_days:
                    days_ahead = i
                    break

            if days_ahead > 0:
                next_run = next_run + timedelta(days=days_ahead)
                # Reset to configured time
                next_run = next_run.replace(
                    hour=cron.get("hour", next_run.hour),
                    minute=cron.get("minute", 0),
                    second=target_second
                )

        # Ensure we're in the future
        if next_run <= now:
            next_run = next_run + timedelta(days=1)

        return next_run

    async def run_pending(self) -> list[str]:
        """
        Run all tasks that are due.

        Returns:
            List of task IDs that were executed
        """
        now = datetime.utcnow()
        executed = []

        for task_id, task in self._tasks.items():
            if task["next_run"] and task["next_run"] <= now:
                success = await self.execute_task(task_id)
                if success:
                    executed.append(task_id)

        return executed

    def start(self) -> None:
        """Start the scheduler."""
        self._running = True
        self.logger.info("scheduler_started", task_count=len(self._tasks))

    def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        self.logger.info("scheduler_stopped")

    @property
    def is_running(self) -> bool:
        """Check if scheduler is running."""
        return self._running


# Global scheduler instance
_scheduler: Optional[TaskScheduler] = None


def get_scheduler() -> TaskScheduler:
    """Get global scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = TaskScheduler()
    return _scheduler
