from fastapi import HTTPException

from app.api.automation import _enqueue_automation_task


class DummyResult:
    id = "task-123"


class DummyTask:
    def __init__(self) -> None:
        self.called_with = None

    def apply_async(self, **kwargs):
        self.called_with = kwargs
        return DummyResult()


class FailingTask:
    def apply_async(self, **kwargs):
        raise RuntimeError("broker down")


def test_enqueue_automation_task_returns_queued_result():
    task = DummyTask()

    result = _enqueue_automation_task(task, "auto_join_groups_task", dry_run=True, max_accounts=3)

    assert task.called_with == {"kwargs": {"dry_run": True, "max_accounts": 3}, "queue": "automation"}
    assert result["queued"] is True
    assert result["status"] == "queued"
    assert result["task_id"] == "task-123"
    assert result["payload"] == {"dry_run": True, "max_accounts": 3}
    assert result["processed"] == 0


def test_enqueue_automation_task_reports_queue_unavailable():
    try:
        _enqueue_automation_task(FailingTask(), "auto_join_groups_task")
    except HTTPException as exc:
        assert exc.status_code == 503
        assert "Automation queue unavailable" in exc.detail
    else:
        raise AssertionError("expected HTTPException")
