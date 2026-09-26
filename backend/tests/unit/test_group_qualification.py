"""Behavioral tests for account-scoped PP-AI qualification."""
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest

from app.api.account_profile_updates import AccountProfileUpdateCreateRequest
from app.modules.account_profile_update.service import operation_snapshot
from app.modules.acquisition.group_qualification import (
    EvidenceCollector,
    evidence_age,
    quality_decision,
    trial_evidence,
)


def good(**kwargs):
    return {"member_count": 50, "valid_messages": 5, "history_complete": True,
            "permissions": {"member": True, "can_send_text": True}, **kwargs}


@pytest.mark.parametrize("snapshot,expected", [
    (good(), "qualified"),
    (good(member_count=49), "qualified"),
    (good(member_count=None), "qualified"),
    (good(valid_messages=4), "qualified"),
    (good(valid_messages=4, history_complete=False), "qualified"),
    (good(valid_messages=4, unknowns=["media_without_readable_caption"]), "qualified"),
    (good(technical_errors=["TimeoutError"]), "technical_wait"),
    (good(protected=True, member_count=1), "protected"),
    (good(permissions={"member": True, "can_send_text": None}), "technical_wait"),
    (good(permissions={"member": True, "can_send_text": False}), "observe"),
    (good(permissions={"member": True, "can_send_text": False, "temporary_until": "later"}), "wait"),
    (good(permissions={"member": True, "can_send_text": True, "slowmode_until": "later"}), "wait"),
    (good(permissions={"member": True, "can_send_text": True, "paid_messages": True}), "observe"),
    (good(rules_incomplete=True), "observe"),
])
def test_quality_boundaries(snapshot, expected):
    assert quality_decision(snapshot)[0] == expected


def ad(sender, text, age=25, role="ordinary", **kwargs):
    return {"source": "recent_promotional_message", "sender_id": sender, "sender_role": role,
            "message_id": sender * 10 if isinstance(sender, int) else None,
            "text": text, "age_hours": age, "accessible": True,
            "warning_search_complete": True, **kwargs}


def test_one_retained_ordinary_ad_can_supply_trial_evidence():
    assert trial_evidence([ad(1, "家具出售联系 https://furniture.example")])
    assert not trial_evidence([ad(1, "家具出售联系 https://furniture.example", age=2)])


@pytest.mark.parametrize("role", ["admin", "bot", "anonymous", "unknown", "system"])
def test_nonordinary_ads_do_not_authorize(role):
    assert not trial_evidence([ad(1, "家具出售联系 https://furniture.example", role=role)])


def test_unverified_warning_or_missing_original_cannot_supply_trial():
    item = ad(1, "家具出售联系 https://furniture.example")
    assert not trial_evidence([{**item, "warning_search_complete": False}])
    assert not trial_evidence([{**item, "warning_reply_ids": [99]}])
    assert not trial_evidence([{**item, "accessible": False}])


def test_system_accounts_cannot_bootstrap_permission():
    assert not trial_evidence([ad(1, "家具出售联系 https://furniture.example", system_account=True)])


def test_old_message_edited_now_is_not_retained_advertising():
    now = datetime.utcnow()
    message = Obj(date=now - timedelta(days=5), edit_date=now - timedelta(minutes=5))
    assert evidence_age(message, now) < 1
    assert not trial_evidence([ad(1, "家具出售联系 https://furniture.example", age=evidence_age(message, now))])

def test_profile_theme_alone_cannot_authorize():
    from app.modules.acquisition.automation import AcquisitionAutomationService
    assert not AcquisitionAutomationService._has_soft_ad_trial_context(
        [{"source": "group_profile", "text": "ChatGPT AI 开发者广告交流群"}])


def test_empty_profile_is_explicit_supported_and_idempotent():
    request = AccountProfileUpdateCreateRequest(account_ids=[2, 3, 4], profile_bio="")
    assert request.profile_bio == ""
    assert operation_snapshot([2, 3, 4], "") == operation_snapshot([4, 3, 2], " ")
    with pytest.raises(ValueError):
        AccountProfileUpdateCreateRequest(account_ids=[2], profile_bio=None)


class Client:
    def __init__(self, messages, *, hidden=False, count=50, pins=None):
        self.history = messages
        self.pins = pins or []
        self.full = Obj(participants_count=count, online_count=2, about="聊天交流", hidden_prehistory=hidden)
        self.send_message = AsyncMock()
        self.send_file = AsyncMock()
    async def __call__(self, request):
        return Obj(full_chat=self.full)
    async def get_permissions(self, entity, who):
        return Obj(is_admin=False, is_creator=False, participant=Obj(banned_rights=None),
                   has_left=False, is_banned=False)
    async def get_entity(self, sender):
        return Obj(id=sender, bot=False)
    async def iter_messages(self, entity, **kwargs):
        source = self.pins if "filter" in kwargs else self.history
        offset = kwargs.get("offset_id", 0)
        source = [m for m in source if not offset or m.id < offset]
        for message in source[:kwargs.get("limit", 100)]:
            yield message


def msg(i, text=None, *, age=1, role=None, edit=None, action=None):
    return Obj(id=i, sender_id=i, sender=Obj(id=i, bot=role == "bot"),
               message=text or f"今天的第{i}条有效讨论消息",
               date=datetime.utcnow() - timedelta(hours=age), edit_date=edit,
               media=None, action=action)


@pytest.mark.asyncio
async def test_sampling_counts_human_messages_and_has_no_remote_mutations():
    client = Client([msg(i) for i in range(10, 15)] + [msg(2, role="bot"), msg(1, action="joined")])
    snapshot = await EvidenceCollector(client).collect(Obj(id=42, megagroup=True))
    assert snapshot["quality_status"] == "qualified"
    assert snapshot["valid_messages"] == 5
    assert snapshot["history_complete"]
    client.send_message.assert_not_called()
    client.send_file.assert_not_called()


@pytest.mark.asyncio
async def test_hidden_history_does_not_fail_the_old_activity_gate():
    client = Client([msg(1)], hidden=True)
    snapshot = await EvidenceCollector(client).collect(Obj(id=42, megagroup=True))
    assert not snapshot["history_complete"]
    assert snapshot["quality_status"] == "qualified"


@pytest.mark.asyncio
async def test_72_hour_boundary_and_duplicate_advertising():
    client = Client([msg(9, "低价出售服务联系 https://shop.example"),
                     msg(8, "优惠出售服务联系 https://shop.example"),
                     msg(7, age=73)])
    snapshot = await EvidenceCollector(client).collect(Obj(id=42, megagroup=True))
    assert snapshot["valid_messages"] == 1
    assert snapshot["quality_status"] == "qualified"


@pytest.mark.asyncio
async def test_all_visible_pins_are_collected():
    client = Client([msg(i) for i in range(10, 15)], pins=[msg(1, "旧广告规则"), msg(2, "最新广告规则")])
    snapshot = await EvidenceCollector(client).collect(Obj(id=42, megagroup=True))
    assert len([e for e in snapshot["evidence"] if e["source"] == "pinned_message"]) == 2
