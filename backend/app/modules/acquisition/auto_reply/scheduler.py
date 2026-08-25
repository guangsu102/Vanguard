"""
Speak Scheduler Module

Schedule management for automatic group messaging.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import structlog

from app.modules.acquisition.config import AcquisitionConfig
from app.modules.acquisition.models import MessageType

logger = structlog.get_logger()


@dataclass
class SpeakTask:
    """Individual speak task in a schedule."""
    group_id: int
    message_type: MessageType
    scheduled_time: Optional[datetime] = None
    interval_minutes: Optional[int] = None
    priority: int = 0


@dataclass
class SpeakSchedule:
    """Schedule for speak tasks."""
    name: str
    tasks: list[SpeakTask] = field(default_factory=list)
    enabled: bool = True
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    _last_execution: Optional[datetime] = field(default=None, repr=False)


class SpeakScheduler:
    """
    Scheduler for automatic group messaging.

    Manages speak schedules with timing, intervals, and priorities.
    """

    def __init__(self, config: Optional[AcquisitionConfig] = None):
        """
        Initialize SpeakScheduler.

        Args:
            config: Optional configuration
        """
        self.config = config or AcquisitionConfig()
        self._schedules: dict[str, SpeakSchedule] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self.logger = logger.bind(module="speak_scheduler")

    async def start(self) -> None:
        """Start the scheduler."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._run_scheduler())
        self.logger.info("scheduler_started")

    async def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.logger.info("scheduler_stopped")

    async def _run_scheduler(self) -> None:
        """Main scheduler loop."""
        while self._running:
            try:
                await self._check_and_execute()
                await asyncio.sleep(60)  # 检查每分钟
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("scheduler_error", error=str(e))
                await asyncio.sleep(60)

    async def _check_and_execute(self) -> None:
        """Check schedules and execute due tasks."""
        now = datetime.utcnow()

        for schedule in self._schedules.values():
            if not schedule.enabled:
                continue

            # 检查时间范围
            if schedule.start_time and now < schedule.start_time:
                continue
            if schedule.end_time and now > schedule.end_time:
                continue

            # 检查间隔
            if schedule._last_execution:
                interval = self._calculate_interval(schedule)
                if (now - schedule._last_execution).total_seconds() < interval:
                    continue

            # 执行
            schedule._last_execution = now

    def _calculate_interval(self, schedule: SpeakSchedule) -> int:
        """Calculate execution interval for a schedule."""
        # 根据优先级调整间隔
        base_interval = self.config.speaker.min_interval_seconds

        if schedule.tasks:
            max_priority = max(t.priority for t in schedule.tasks)
            if max_priority > 0:
                return max(30, base_interval - max_priority * 5)

        return base_interval

    def add_schedule(self, schedule: SpeakSchedule) -> None:
        """
        Add a schedule.

        Args:
            schedule: Schedule to add
        """
        self._schedules[schedule.name] = schedule
        self.logger.info("schedule_added", name=schedule.name, tasks=len(schedule.tasks))

    def remove_schedule(self, name: str) -> bool:
        """
        Remove a schedule.

        Args:
            name: Schedule name

        Returns:
            True if removed
        """
        if name in self._schedules:
            del self._schedules[name]
            self.logger.info("schedule_removed", name=name)
            return True
        return False

    def get_schedule(self, name: str) -> Optional[SpeakSchedule]:
        """Get a schedule by name."""
        return self._schedules.get(name)

    def should_execute_task(self, schedule: SpeakSchedule) -> bool:
        """
        Check if a schedule's task should be executed.

        Args:
            schedule: Schedule to check

        Returns:
            True if should execute
        """
        if not schedule.enabled:
            return False

        now = datetime.utcnow()

        # 检查时间范围
        if schedule.start_time and now < schedule.start_time:
            return False
        if schedule.end_time and now > schedule.end_time:
            return False

        return True

    async def create_level_schedule(
        self,
        name: str,
        groups: list[dict],
        message_type: MessageType = MessageType.INTERACTION,
    ) -> SpeakSchedule:
        """
        Create a schedule for groups by level.

        Args:
            name: Schedule name
            groups: List of group dicts with 'group_id'
            message_type: Message type to use

        Returns:
            Created SpeakSchedule
        """
        tasks = []
        for group in groups:
            task = SpeakTask(
                group_id=group["group_id"],
                message_type=message_type,
                priority=group.get("priority", 0),
            )
            tasks.append(task)

        schedule = SpeakSchedule(name=name, tasks=tasks)

        # 根据等级设置间隔
        if "A级" in name or "A级" in name:
            schedule.enabled = True
        elif "B级" in name:
            schedule.enabled = True

        self.add_schedule(schedule)
        return schedule

    def get_due_tasks(self) -> list[SpeakTask]:
        """
        Get all tasks that are due for execution.

        Returns:
            List of due tasks
        """
        now = datetime.utcnow()
        due_tasks = []

        for schedule in self._schedules.values():
            if not self.should_execute_task(schedule):
                continue

            for task in schedule.tasks:
                if self._is_task_due(task, now):
                    due_tasks.append(task)

        # 按优先级排序
        due_tasks.sort(key=lambda t: t.priority, reverse=True)
        return due_tasks

    def _is_task_due(self, task: SpeakTask, now: datetime) -> bool:
        """Check if a task is due."""
        if not task.scheduled_time:
            return True

        return now >= task.scheduled_time

    async def execute_task(
        self,
        task: SpeakTask,
        speaker,  # Speaker instance
    ) -> bool:
        """
        Execute a speak task.

        Args:
            task: Task to execute
            speaker: Speaker instance for sending messages

        Returns:
            True if successful
        """
        try:
            self.logger.info("executing_task", group_id=task.group_id)

            # 调度到 Speaker 执行
            result = await speaker.speak_in_group(
                group_id=task.group_id,
                message="",  # Speaker 会自动生成内容
            )

            return result.success

        except Exception as e:
            self.logger.error("task_execution_failed", group_id=task.group_id, error=str(e))
            return False
