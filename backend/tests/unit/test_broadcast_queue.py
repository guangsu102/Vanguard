import importlib
import json

import pytest


broadcasts_api = importlib.import_module("app.api.broadcasts")


class DummyResult:
    id = "broadcast-task-123"


@pytest.mark.asyncio
async def test_broadcast_execute_is_queued(test_db, monkeypatch):
    broadcast = broadcasts_api.BroadcastRecord(
        content="hello",
        broadcast_type="node_update",
        target_groups=json.dumps([-100123, -100456]),
        target_group_count=2,
        status="pending",
    )
    test_db.add(broadcast)
    await test_db.commit()
    await test_db.refresh(broadcast)

    calls = {}

    def fake_apply_async(*args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return DummyResult()

    monkeypatch.setattr(broadcasts_api.execute_broadcast_record, "apply_async", fake_apply_async)

    response = await broadcasts_api.execute_broadcast(broadcast.id, db=test_db)
    await test_db.refresh(broadcast)

    assert calls["kwargs"] == {"args": [broadcast.id], "queue": "broadcast"}
    assert broadcast.status == "queued"
    assert response["data"]["queued"] is True
    assert response["data"]["task_name"] == "execute_broadcast_record"
    assert response["data"]["task_id"] == "broadcast-task-123"
