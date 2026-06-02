from app.core.worker_status import TelegramWorkerStatusValue
from app.workers.telegram_worker import TelegramWorker, TelegramWorkerRole


def test_growth_worker_status_degraded_without_accounts():
    worker = TelegramWorker(TelegramWorkerRole.GROWTH_USER, worker_id="test-growth")

    assert worker._status_for_snapshot({"enabled_accounts": 0}) == TelegramWorkerStatusValue.DEGRADED.value


def test_growth_worker_status_degraded_without_runtime_capable_accounts():
    worker = TelegramWorker(TelegramWorkerRole.GROWTH_USER, worker_id="test-growth")

    status = worker._status_for_snapshot(
        {"enabled_accounts": 1, "runtime": {"runtime_capable_accounts": 0}}
    )

    assert status == TelegramWorkerStatusValue.DEGRADED.value


def test_growth_worker_status_online_with_enabled_account():
    worker = TelegramWorker(TelegramWorkerRole.GROWTH_USER, worker_id="test-growth")

    assert worker._status_for_snapshot({"enabled_accounts": 1}) == TelegramWorkerStatusValue.ONLINE.value


def test_guardian_worker_status_degraded_when_all_bots_fail():
    worker = TelegramWorker(TelegramWorkerRole.GUARDIAN_BOT, worker_id="test-guardian")

    status = worker._status_for_snapshot({"enabled_bots": 2, "runtime": {"failed_bots": 2}})

    assert status == TelegramWorkerStatusValue.DEGRADED.value


def test_worker_event_flag_supports_property_and_method_shapes():
    property_event = type("PropertyEvent", (), {"user_joined": True})()
    method_event = type("MethodEvent", (), {"user_joined": lambda self: True})()

    assert TelegramWorker._event_flag(property_event, "user_joined") is True
    assert TelegramWorker._event_flag(method_event, "user_joined") is True
