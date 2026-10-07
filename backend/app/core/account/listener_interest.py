"""Project advertising-relevant facts without archiving third-party chatter.

The projection never advances Telegram cursors and never performs a Telegram
request. Protocol processing remains with the SDK; facts survive independently.
"""

from __future__ import annotations

import copy
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from telethon import utils
from telethon.tl import types


def fresh(policy: dict | None) -> bool:
    return bool(policy and policy.get("selective") and time.monotonic() - policy["loaded_at"] <= 90)


async def load_interests(db: Any) -> dict:
    from app.core.group.models import GroupAccountMembership
    from app.modules.acquisition.adaptive_frequency import canonical, payload
    from app.modules.acquisition.models import AdDeliveryLog, GroupQualificationAudit

    accounts: dict[int, dict] = {}

    def entry(account_id):
        return accounts.setdefault(account_id, {"ads": {}, "groups": {}, "verification": {}})

    now = datetime.utcnow()
    audits = (await db.scalars(select(GroupQualificationAudit).where(
        GroupQualificationAudit.decision.in_(["allowed", "trial"]),
        GroupQualificationAudit.state != "cancelled",
    ).order_by(GroupQualificationAudit.id.desc()))).all()
    approved = {}
    for audit in audits:
        approved.setdefault(audit.membership_id, audit)
    members = (
        await db.scalars(
            select(GroupAccountMembership).where(
                GroupAccountMembership.status.in_(["joined", "pending"]),
            )
        )
    ).all()
    for member in members:
        peer = int(member.telegram_group_id)
        # Legacy positive IDs are uncertain identities; retain both candidates
        # for review cues, never use them alone as proof of an ad deletion.
        peers = [peer] if peer < 0 else [-peer, -(10**12 + peer)]
        value = {
            "membership_id": member.id,
            "joined_at": member.joined_at.isoformat() if member.joined_at else None,
            "evidence_needed": member.review_status != "approved",
        }
        audit = approved.get(member.id)
        snapshot = payload(audit.evidence_json) if audit and audit.membership_joined_at == member.joined_at else {}
        value["watched_evidence"] = [item["message_id"] for item in snapshot.get("evidence", [])
            if type(item.get("message_id")) is int and (item.get("source") in {"pinned_message", "admin_rule"}
                or (snapshot.get("authorization_basis") != "verified_own_delivery_survived"
                    and item.get("source") == "recent_promotional_message"))]
        for peer in peers:
            entry(member.account_id)["groups"][peer] = value
            if (
                member.joined_at
                and member.joined_at >= now - timedelta(hours=48)
                and member.review_status
                in {"initial_pending", "review_2h", "review_24h", "verification_pending"}
            ):
                entry(member.account_id)["verification"][peer] = value
    logs = (
        await db.scalars(
            select(AdDeliveryLog).where(
                AdDeliveryLog.status == "success",
                AdDeliveryLog.telegram_message_id.is_not(None),
            )
        )
    ).all()
    for log in logs:
        peer = canonical(log.telegram_group_id, payload(log.qualification_context_json))
        if peer is not None:
            entry(log.account_id)["ads"].setdefault(peer, set()).add(int(log.telegram_message_id))
    return accounts


def account_policy(policy: dict | None, account_id: int, client: Any = None) -> dict | None:
    if not policy:
        return None
    own = policy.get("interests", {}).get(account_id, {"ads": {}, "groups": {}, "verification": {}})
    self_id = getattr(getattr(client, "_mb_entity_cache", None), "self_id", None)
    return {**policy, **own, "account_id": account_id, "self_id": self_id}


def peer_id(message: Any) -> int | None:
    peer = getattr(message, "peer_id", None)
    return utils.get_peer_id(peer) if peer is not None else None


def full_consumer(peer: int | None, policy: dict) -> bool:
    raw = (-peer - 10**12 if peer < -(10**12) else -peer) if peer is not None and peer < 0 else peer
    return bool(
        peer is None
        or peer > 0
        or peer in policy.get("owned_chats", set())
        or raw in policy.get("owned_chats", set())
        or policy.get("ordinary_required")
    )


def verification_prompt(message: Any, policy: dict) -> bool:
    member = policy.get("verification", {}).get(peer_id(message))
    if not member or getattr(message, "out", False):
        return False
    created = getattr(message, "date", None)
    if not isinstance(created, datetime):
        return False
    created = created.replace(tzinfo=UTC) if created.tzinfo is None else created.astimezone(UTC)
    joined = datetime.fromisoformat(member["joined_at"]).replace(tzinfo=UTC)
    now = datetime.now(UTC)
    if (
        not max(joined - timedelta(seconds=30), now - timedelta(minutes=15))
        <= created
        <= now + timedelta(minutes=1)
    ):
        return False
    from app.modules.acquisition.automation import VERIFICATION_SIGNAL_RE

    return bool(VERIFICATION_SIGNAL_RE.search(getattr(message, "message", "") or ""))


def project(envelope: Any, policy: dict | None) -> tuple[Any, list[int], list[dict]]:
    """Return SDK envelope, skipped business indices, and durable small facts."""
    if not fresh(policy):
        return envelope, [], []
    if isinstance(envelope, types.UpdateShortChatMessage):
        message = types.Message(
            id=envelope.id,
            peer_id=types.PeerChat(envelope.chat_id),
            from_id=types.PeerUser(envelope.from_id),
            date=envelope.date,
            message=envelope.message,
            out=envelope.out,
            mentioned=envelope.mentioned,
            reply_to=envelope.reply_to,
            entities=envelope.entities,
        )
        envelope = types.UpdateShort(
            types.UpdateNewMessage(message, envelope.pts, envelope.pts_count), envelope.date
        )
    from app.core.account.listener_protocol import updates_in

    updates = updates_in(envelope)
    projected, ignored, facts = [], [], []
    stamp = getattr(envelope, "date", None)
    occurred = stamp.timestamp() if isinstance(stamp, datetime) else time.time()
    ads = policy.get("ads", {})
    groups = policy.get("groups", {})

    def fact(kind, peer, suffix="", **data):
        if kind == "rules" and data.get("message_ids"):
            suffix = str(max(data["message_ids"]) % 32)
        facts.append(
            {"key": f"{kind}:{peer}:{suffix}", "kind": kind, "peer": peer, "at": occurred, **data}
        )

    for index, update in enumerate(updates):
        handled = False
        message = getattr(update, "message", None)
        peer = peer_id(message)
        if isinstance(
            update,
            (
                types.UpdateNewChannelMessage,
                types.UpdateNewMessage,
                types.UpdateEditChannelMessage,
                types.UpdateEditMessage,
            ),
        ):
            if not full_consumer(peer, policy):
                if isinstance(message, types.Message):
                    member = groups.get(peer, {})
                    from app.modules.acquisition.group_qualification import possible_rule
                    if member and not message.out and (
                        message.id in member.get("watched_evidence", [])
                        or possible_rule(message.message or "")
                    ):
                        changed_at = message.edit_date or message.date
                        fact("rules", peer, joined_at=member.get("joined_at"),
                             message_ids=[message.id],
                             at=changed_at.timestamp() if isinstance(changed_at, datetime) else occurred)
                    if member.get("evidence_needed") and not message.out:
                        from app.modules.acquisition.group_qualification import looks_like_ad, naive
                        created = naive(message.date)
                        if (created and datetime.utcnow() - timedelta(hours=72) <= created <= datetime.utcnow()
                                and looks_like_ad(message.message or "")):
                            # At most 32 pending hints per group in the durable
                            # ingress store; only IDs/dates, never chat bodies.
                            fact("evidence_candidate", peer, str(message.id % 32),
                                 joined_at=member.get("joined_at"), message_id=message.id,
                                 message_date=created.isoformat())
                    if verification_prompt(message, policy):
                        fact(
                            "verification",
                            peer,
                            joined_at=policy["verification"][peer]["joined_at"],
                        )
                    light = types.Message(
                        id=message.id,
                        peer_id=message.peer_id,
                        date=message.date,
                        message="",
                        from_id=message.from_id,
                        out=message.out,
                    )
                    update = copy.copy(update)
                    update.message = light
                    handled = True
                elif isinstance(message, types.MessageService):
                    action = message.action
                    if peer in groups and isinstance(action, types.MessageActionPinMessage):
                        fact("rules", peer, joined_at=groups[peer].get("joined_at"))
                    affected = list(getattr(action, "users", []) or [])
                    affected.append(getattr(action, "user_id", None))
                    if isinstance(
                        action,
                        (
                            types.MessageActionChatJoinedByLink,
                            types.MessageActionChatJoinedByRequest,
                        ),
                    ):
                        affected.append(getattr(message.from_id, "user_id", None))
                    if policy.get("self_id") in affected and policy.get("self_id") is not None:
                        # Old successful joins must not revoke an already
                        # approved membership when history is projected.
                        kind = (
                            "membership"
                            if isinstance(action, types.MessageActionChatDeleteUser)
                            else "verification"
                        )
                        event_at = (
                            message.date.timestamp()
                            if isinstance(message.date, datetime)
                            else occurred
                        )
                        fact(
                            kind, peer, at=event_at, joined_at=groups.get(peer, {}).get("joined_at")
                        )
                    handled = True
        elif isinstance(update, types.UpdateDeleteChannelMessages):
            peer = -(10**12 + update.channel_id)
            if not full_consumer(peer, policy):
                watched = ads.get(peer, set())
                for mid in sorted(watched.intersection(update.messages)):
                    fact("deleted", peer, str(mid), message_id=mid)
                # A successful send can race the policy refresh. A single cue
                # checks the DB's latest ad; never archive all unknown IDs.
                if peer in groups:
                    evidence_deleted = set(groups[peer].get("watched_evidence", [])).intersection(update.messages)
                    if evidence_deleted:
                        fact("rules", peer, joined_at=groups[peer].get("joined_at"),
                             message_ids=sorted(evidence_deleted))
                    unknown = sorted(set(update.messages) - watched)
                    if unknown:
                        fact(
                            "deletion_candidates",
                            peer,
                            ids=unknown[-512:],
                            overflow=len(unknown) > 512,
                        )
                handled = True
        elif isinstance(update, types.UpdateDeleteMessages):
            # Basic chats/private chats share one namespace; channel IDs cannot
            # be matched against these unscoped message IDs.
            for target, mids in ads.items():
                if -(10**12) < target < 0:
                    for mid in sorted(mids.intersection(update.messages)):
                        fact("deleted", target, str(mid), message_id=mid)
            handled = True
        elif isinstance(update, types.UpdateChannelParticipant):
            peer = -(10**12 + update.channel_id)
            if not full_consumer(peer, policy):
                if update.user_id == policy.get("self_id"):
                    if isinstance(update.date, datetime):
                        occurred = update.date.timestamp()
                    participant = update.new_participant
                    rights = getattr(participant, "banned_rights", None)
                    blocked = (
                        participant is None
                        or isinstance(participant, types.ChannelParticipantLeft)
                        or bool(
                            getattr(participant, "left", False)
                            or getattr(rights, "send_messages", False)
                            or getattr(rights, "send_plain", False)
                            or getattr(rights, "view_messages", False)
                        )
                    )
                    fact(
                        "membership" if peer in groups else "verification",
                        peer,
                        joined_at=groups.get(peer, {}).get("joined_at"),
                        blocked=blocked,
                    )
                elif peer in groups and any(isinstance(p, (types.ChannelParticipantAdmin, types.ChannelParticipantCreator))
                                             for p in (update.prev_participant, update.new_participant)):
                    # A rule author's promotion/demotion invalidates partial
                    # observations; ordinary member joins do not create work.
                    fact('rules', peer, joined_at=groups[peer].get('joined_at'))
                handled = True
        elif isinstance(update, (types.UpdateChannel, types.UpdateChannelTooLong)):
            peer = -(10**12 + update.channel_id)
            if not full_consumer(peer, policy):
                if peer in groups:
                    fact("gap", peer, joined_at=groups[peer].get("joined_at"))
                handled = True
        elif isinstance(update, (types.UpdatePinnedChannelMessages, types.UpdatePinnedMessages)):
            peer = (-(10**12 + update.channel_id) if hasattr(update, "channel_id")
                    else utils.get_peer_id(update.peer))
            if peer in groups:
                fact("rules", peer, joined_at=groups[peer].get("joined_at"),
                     message_ids=list(update.messages)[-32:])
            handled = not full_consumer(peer, policy)
        elif isinstance(update, types.UpdatesTooLong):
            for target, member in groups.items():
                fact("gap", target, joined_at=member.get("joined_at"))
            handled = True
        elif isinstance(update, types.UpdateChatDefaultBannedRights):
            peer = utils.get_peer_id(update.peer)
            if peer in groups:
                fact("membership", peer, joined_at=groups[peer]["joined_at"])
            handled = not full_consumer(peer, policy)
        elif isinstance(
            update,
            (
                types.UpdateUserStatus,
                types.UpdateUserTyping,
                types.UpdateChatUserTyping,
                types.UpdateReadChannelInbox,
                types.UpdateReadChannelOutbox,
                types.UpdateReadHistoryInbox,
                types.UpdateReadHistoryOutbox,
                types.UpdateMessagePoll,
                types.UpdateChannelWebPage,
                types.UpdatesTooLong,
            ),
        ):
            handled = True
        elif not isinstance(update, (types.UpdateShortMessage, types.UpdateShortSentMessage)):
            # Unsupported/non-business update types still reach the SDK. None
            # of our registered business callbacks consumes their raw bodies.
            handled = not hasattr(update, "message")
        if handled:
            update._vanguard_ignore_business = True
            ignored.append(index)
        projected.append(update)
    if hasattr(envelope, "updates"):
        result = copy.copy(envelope)
        result.updates = projected
    elif hasattr(envelope, "update"):
        result = copy.copy(envelope)
        result.update = projected[0]
    else:
        result = projected[0]
    return result, ignored, facts
