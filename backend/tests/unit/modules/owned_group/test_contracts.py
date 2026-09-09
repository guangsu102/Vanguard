import pytest

from app.modules.owned_group.contracts import (
    DEFAULT_OPERATION_CONFIG,
    ItemStatus,
    OperationStatus,
    ResourceType,
    assert_transition,
    build_config_snapshot,
    build_selection_snapshot,
)


def test_selection_snapshot_is_deduplicated_and_stable() -> None:
    selection, digest = build_selection_snapshot(
        [
            {"resource_type": ResourceType.BOT.value, "resource_id": 2},
            {"resource_type": ResourceType.USER.value, "resource_id": 1},
        ]
    )

    assert selection == [
        {"resource_type": "bot", "resource_id": 2},
        {"resource_type": "user", "resource_id": 1},
    ]
    assert len(digest) == 64


def test_selection_snapshot_rejects_duplicate_resources() -> None:
    with pytest.raises(ValueError, match="Duplicate resource"):
        build_selection_snapshot(
            [
                {"resource_type": "user", "resource_id": 1},
                {"resource_type": "user", "resource_id": 1},
            ]
        )


def test_config_snapshot_applies_defaults_and_hashes() -> None:
    config, digest = build_config_snapshot({"batch_size": 10})

    assert config["batch_size"] == 10
    assert config["batch_interval_seconds"] == DEFAULT_OPERATION_CONFIG["batch_interval_seconds"]
    assert len(digest) == 64


def test_config_snapshot_rejects_parallelism_above_one() -> None:
    with pytest.raises(ValueError, match="max_parallelism"):
        build_config_snapshot({"max_parallelism": 2})


def test_operation_state_transitions_are_explicit() -> None:
    assert_transition("operation", OperationStatus.QUEUED.value, OperationStatus.RUNNING.value)
    assert_transition("item", ItemStatus.MEMBER_VERIFIED.value, ItemStatus.ADMIN_PROMOTING.value)

    with pytest.raises(ValueError, match="Invalid operation transition"):
        assert_transition(
            "operation", OperationStatus.COMPLETED.value, OperationStatus.RUNNING.value
        )
