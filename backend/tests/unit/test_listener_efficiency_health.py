import json
from datetime import datetime
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock
import pytest
from app.core.worker_status import TelegramWorkerStatus
from app.core.runtime_health import operational_snapshot

@pytest.mark.asyncio
async def test_connected_but_delayed_sync_is_visible_as_warning(test_db, monkeypatch):
    from app.core import redis as module
    now = datetime.utcnow()
    async def get(key):
        return now.isoformat() if key == 'vanguard:runtime_recovery:last_run_at' else json.dumps({'alerts': []})
    monkeypatch.setattr(module, 'get_redis', AsyncMock(return_value=Obj(get=get, info=AsyncMock(return_value={}))))
    for role in ['growth_user_worker', 'guardian_bot_worker']:
        test_db.add(TelegramWorkerStatus(worker_id=role, role=role, status='running', last_heartbeat_at=now,
            metadata_json=json.dumps({'runtime': {'listeners': [{'account_id': 2, 'connected': True,
                'state': 'connected_wait', 'sync_wait_reason': 'telegram_read_budget', 'read_wait_seconds': 130}]}})))
    await test_db.commit()
    result = await operational_snapshot(test_db)
    assert result['status'] == 'warning'
    assert {'code': 'listener_sync_delayed', 'account_id': 2, 'severity': 'warning'} in result['alerts']
