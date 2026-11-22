"""A sufficient proof must survive every consumer without inventing rule facts."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
from telethon import types

from app.modules.acquisition import qualification_service as service
from app.modules.acquisition.automation import GroupAdRulesAuditResult
from app.modules.acquisition.group_qualification import EvidenceCollector
from tests.unit.test_qualification_read_context import FastPathClient, root
from tests.unit.test_qualification_service import setup


@pytest.mark.parametrize("payload,decision,expected", [
    ({}, "unknown", False),
    ({"decision": "observe"}, "observe", False),
    ({}, "reject", True),
    ({"qualification_exit_history": [{"reason": "members_below_50"}]}, "unknown", True),
    ({"previous_checks": [{"decision": "reject"}]}, "unknown", True),
    ({"exit_group_rule_ban": True, "collected_at": "2026-11-03T00:00:00"}, "unknown", True),
])
def test_current_queue_is_not_negative_reentry_history(payload, decision, expected):
    row = Obj(decision=decision, evidence_json=json.dumps(payload))
    assert service.fast_path_history_requires_full([(Obj(), row)]) is expected


@pytest.mark.asyncio
async def test_full_service_keeps_fast_trial_and_not_evaluated_rules(test_db, monkeypatch):
    account, group, member, row = await setup(test_db, decision="unknown")
    entity = types.Channel(id=group.group_id, title="test", photo=types.ChatPhotoEmpty(),
                           date=datetime.utcnow(), megagroup=True)
    message = root()
    message.date = datetime.utcnow() - timedelta(hours=30)
    client = FastPathClient([message])
    client.get_entity = AsyncMock(return_value=entity)
    client.get_me = AsyncMock(return_value=Obj(id=200))
    async def permissions(_client, _entity, sender, **_kwargs):
        return await client.get_permissions(_entity, sender)
    monkeypatch.setattr("app.modules.acquisition.qualification_reads.current_permissions", permissions)
    monkeypatch.setattr(service.settings, "REVIEW_READ_OPTIMIZATION_ENABLED", True)
    monkeypatch.setattr(service.settings, "REVIEW_READ_OPTIMIZATION_ACCOUNT_IDS", "2")
    monkeypatch.setattr(service.settings, "REVIEW_READ_FAST_PATH", True)
    monkeypatch.setattr("app.core.account.read_schedule.check_read_ready", AsyncMock())
    monkeypatch.setattr("app.modules.acquisition.callback_evidence.hint_ids", AsyncMock(return_value=[7]))
    collectors = []

    def collect_factory(*args, **kwargs):
        collector = EvidenceCollector(*args, **kwargs)
        collectors.append(collector)
        return collector

    monkeypatch.setattr(service, "EvidenceCollector", collect_factory)
    fake = Obj(db=test_db, account_pool=Obj(
        add_account_from_db=AsyncMock(),
        acquire_by_id=AsyncMock(return_value=Obj(client=client)), release=AsyncMock()),
        _join_review_account_block_reason=lambda *_: None,
        _telegram_entity_matches_group_id=lambda *_: True,
        _evaluate_group_ad_rules=lambda evidence: GroupAdRulesAuditResult(evidence=evidence),
    )
    result = await service.assess(fake, account.id, group, row=row)
    payload = json.loads(row.evidence_json)
    assert collectors[0].enable_fast_path is True
    assert payload.get("technical_errors") == []
    assert result.passed and row.decision == "trial"
    assert payload["sampling_stopped_reason"] == "sufficient_ordinary_root_trial_proof"
    assert payload["exit_group_rule_ban"] is None
    assert payload["group_level_advertising_ban"] is None
    assert payload["rules_evaluation_status"] == "NOT_EVALUATED"
    assert not payload.get("ai_pending")
    assert client.iter_calls == []


def test_fast_collection_cannot_be_reused_as_complete_ai_material(monkeypatch):
    from app.modules.acquisition.qualification_cache import evidence_is_fresh

    now = datetime.utcnow()
    snapshot = {"collected_at": now.isoformat(), "collection_complete": True,
                "quality_status": "qualified", "evidence": [{"text": "test"}],
                "rules_evaluation_status": "NOT_EVALUATED"}
    assert not evidence_is_fresh(snapshot, now)
    monkeypatch.setattr(service, "checkpoints_enabled", lambda: True)
    monkeypatch.setattr(service, "checkpoint_payload", lambda *_args, **_kwargs: snapshot)
    assert not service.checkpoint_collection_current(Obj(decision="unknown"), snapshot, now)
