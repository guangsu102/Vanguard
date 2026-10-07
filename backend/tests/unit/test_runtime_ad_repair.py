import asyncio
import hashlib
import json
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
from telethon.tl import types

from app.core.account.listener_storage import read_listener_storage
from app.modules.acquisition import read_costs
from app.modules.acquisition.capacity import inventory_snapshot
from app.modules.acquisition.models import AdCampaign, AdDeliveryLog
from app.modules.acquisition.receipt_identity import verified_receipt_identity


def test_offline_storage_is_read_only_and_has_no_payloads(tmp_path):
    path = tmp_path / "offline.session"
    now = time.time()
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE vanguard_raw_updates(id INTEGER,received_at REAL,payload BLOB,state TEXT)"
        )
        db.execute(
            "INSERT INTO vanguard_raw_updates VALUES(1,?,?,?)",
            (now - 300, b"private-body", "pending"),
        )
        db.execute(
            "CREATE TABLE update_state(id INTEGER,pts INTEGER,qts INTEGER,date REAL,seq INTEGER)"
        )
        db.execute("INSERT INTO update_state VALUES(0,100,0,?,1)", (now - 400,))
    before = hashlib.sha256(path.read_bytes()).digest()
    result = read_listener_storage(path)
    assert result["states"] == {"pending": 1}
    assert result["oldest_pending_seconds"] >= 300 and result["checkpoint"]["count"] == 1
    assert "private-body" not in json.dumps(result) and str(path) not in json.dumps(result)
    assert hashlib.sha256(path.read_bytes()).digest() == before
    missing = tmp_path / "missing.session"
    assert read_listener_storage(missing)["available"] is False and not missing.exists()
    assert "states" not in read_listener_storage(missing)


@pytest.mark.asyncio
async def test_deferred_bootstrap_still_reports_disk_backlog(monkeypatch):
    from app.core.account import rpc_governor
    from app.core.worker_status import TelegramWorkerRole
    from app.workers import telegram_worker as module

    @asynccontextmanager
    async def db():
        yield Obj()

    monkeypatch.setattr(module, "get_db_session", db)
    monkeypatch.setattr(
        rpc_governor,
        "check_read_ready",
        AsyncMock(side_effect=rpc_governor.RpcDeferred("telegram_read_budget", 100)),
    )
    storage = {
        "available": True,
        "source": "disk",
        "states": {"pending": 58453},
        "checkpoint": {"count": 51},
    }
    pool = Obj(
        get_account_by_id=AsyncMock(return_value=Obj(client=None)),
        listener_storage_snapshot=AsyncMock(return_value=storage),
        connect_by_id=AsyncMock(),
    )
    worker = module.TelegramWorker(TelegramWorkerRole.GROWTH_USER)
    worker._account_pool = pool
    result = await worker._ensure_growth_listeners([Obj(id=2)])
    assert result["listeners"][0]["raw_journal"] == storage
    assert result["listeners"][0]["state"] == "deferred" and result["active_listeners"] == 0
    pool.connect_by_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_health_warns_on_disk_backlog_without_an_sdk_client(test_db, monkeypatch):
    from app.core import redis as module
    from app.core.runtime_health import operational_snapshot
    from app.core.worker_status import TelegramWorkerStatus

    now = datetime.utcnow()

    async def get(key):
        return (
            now.isoformat()
            if key == "vanguard:runtime_recovery:last_run_at"
            else json.dumps({"alerts": []})
        )

    monkeypatch.setattr(
        module, "get_redis", AsyncMock(return_value=Obj(
            get=get, info=AsyncMock(return_value={}), zrangebyscore=AsyncMock(return_value=[]),
            zcount=AsyncMock(return_value=0), hgetall=AsyncMock(return_value={}),
        ))
    )
    for role in ["growth_user_worker", "guardian_bot_worker"]:
        test_db.add(
            TelegramWorkerStatus(
                worker_id=role,
                role=role,
                status="online",
                last_heartbeat_at=now,
                metadata_json=json.dumps(
                    {
                        "runtime": {
                            "listeners": [
                                {
                                    "account_id": 2,
                                    "connected": False,
                                    "state": "deferred",
                                    "raw_journal": {
                                        "states": {"pending": 58453},
                                        "oldest_pending_seconds": 300,
                                    },
                                }
                            ]
                        }
                    }
                ),
            )
        )
    await test_db.commit()
    result = await operational_snapshot(test_db)
    assert {"code": "listener_update_backlog", "severity": "warning", "account_id": 2} in result[
        "alerts"
    ]


@pytest.mark.asyncio
async def test_legacy_receipt_excluded_until_identity_is_proven(test_db):
    from tests.unit.test_qualification_service import setup

    account, group, member, review = await setup(test_db)
    campaign = AdCampaign(name="receipt", enabled=True)
    test_db.add(campaign)
    await test_db.flush()
    now = datetime.utcnow()
    log = AdDeliveryLog(
        account_id=account.id,
        group_id=group.id,
        telegram_group_id=group.group_id,
        ad_campaign_id=campaign.id,
        status="success",
        telegram_message_id=11,
        sent_at=now - timedelta(hours=2),
        survival_status="survived",
    )
    test_db.add(log)
    await test_db.commit()
    blocked = await inventory_snapshot(test_db, account.id, now)
    assert blocked["qualified"] == 1 and blocked["usable"] == 0 and blocked["identity_blocked"] == 1
    log.qualification_context_json = json.dumps(
        {"group_type": "supergroup", "telegram_group_id": -1000000000000 - group.group_id}
    )
    await test_db.commit()
    ready = await inventory_snapshot(test_db, account.id, now)
    assert ready["usable"] == 1 and ready["slots_24h"] == 1 and ready["identity_blocked"] == 0


def receipt_fixture():
    now = datetime.now(UTC)
    log = Obj(
        id=21,
        account_id=2,
        telegram_group_id=123,
        telegram_message_id=77,
        status="success",
        sent_at=now,
    )
    peer = types.PeerChannel(123)
    message = Obj(id=77, sender_id=999, peer_id=peer, date=now)
    return log, peer, message


def test_exact_receipt_identity_and_rejection_of_guesses():
    log, peer, message = receipt_fixture()
    evidence = verified_receipt_identity(log, peer, message, 999)
    assert evidence["telegram_group_id"] == -1000000000123
    for change in (
        {"sender_id": 1},
        {"id": 78},
        {"peer_id": types.PeerChat(123)},
        {"date": log.sent_at + timedelta(hours=1)},
    ):
        with pytest.raises(ValueError):
            verified_receipt_identity(log, peer, Obj(**(vars(message) | change)), 999)
    with pytest.raises(ValueError):
        verified_receipt_identity(log, peer, None, 999)


def test_cost_estimator_needs_samples_and_accounts_for_failed_reads():
    now = time.time()
    samples = [json.dumps({"at": now, "reads": 4, "complete": True})] * 20
    assert read_costs.estimate(samples[:19], 12, now) == (12, 19, False)
    assert read_costs.estimate(samples, 12, now) == (4, 20, True)
    samples += [json.dumps({"at": now, "reads": 40, "complete": False})] * 10
    assert read_costs.estimate(samples, 12, now) == (24, 20, True)
    assert read_costs.estimate(samples, 12, now + 8 * 86400) == (12, 0, False)


@pytest.mark.asyncio
async def test_read_costs_are_isolated_and_do_not_fail_delivery(monkeypatch):
    captured = []

    async def save(meter):
        captured.append((meter.account_id, meter.reads, meter.complete))
        raise RuntimeError("optional telemetry unavailable")

    monkeypatch.setattr(read_costs, "_save", save)

    async def run(aid):
        async with read_costs.measure(aid, "delivery") as meter:
            read_costs.charge_reads(aid, 3, "ad")
            read_costs.charge_reads(aid, 30, "sync")
            read_costs.charge_reads(aid + 10, 30, "ad")
            await asyncio.sleep(0)
            meter.complete = True

    await asyncio.gather(run(2), run(3))
    assert sorted(captured) == [(2, 3, True), (3, 3, True)]
