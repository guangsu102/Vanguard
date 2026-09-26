"""Account-scoped read-only Telegram evidence and qualification rules."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

POLICY_VERSION = "pp-ai-qualification-v3"
WINDOW_HOURS = 72
MAX_MESSAGES, MAX_IDENTITIES, MAX_PINS, MAX_AD_EVIDENCE = 1000, 60, 200, 100
MAX_WARNING_THREAD_ADS, MAX_WARNING_THREAD_REPLIES = 6, 50
MAX_PRIORITY_IDENTITIES = 40
URL = re.compile(
    r"(?:https?://|t\.me/|telegram\.me/|www\.|(?<![\w@])(?:[a-z0-9-]+\.)+[a-z]{2,}\b)[^\s<>]*",
    re.I,
)
CONTACT = re.compile(
    r"(?<![\w/])@([a-z0-9_]{4,})|(?:微信|wechat|wx|qq)\s*[:：号]?\s*([a-z0-9_-]{5,})", re.I
)
OFFER = re.compile(
    r"出售|售卖|低价|优惠|折扣|套餐|代充|订阅|购买|价格|特价|招商|批发|出租|现货|接单|服务|合作|试用|sale|discount|price",
    re.I,
)
CTA = re.compile(
    r"私聊|联系|咨询|下单|注册|试用|看.{0,3}(?:资料|主页|简介)|点.{0,3}头像|有需要|https?://|t\.me/|@[a-z0-9_]{4,}|contact|order",
    re.I,
)
RULE = re.compile(r"群规|群規|本群|公告|禁止|允许|广告|廣告|推广|推廣|rules|advert|promotion", re.I)
WARNING = re.compile(
    r"警告|删除|刪除|删帖|禁言|封禁|踢出|禁止.{0,8}(?:广告|推广)|warn|removed|deleted|muted|banned|spam",
    re.I,
)
RULE_DIRECTIVE = re.compile(
    r"群规|群規|本群|公告|禁止|不许|不允許|不允许|不得|允许|允許|"
    r"(?:广告|廣告|推广|推廣).{0,8}(?:仅|只|必须|需|收费|付费|审批|白名单)|"
    r"rules|prohibit|approval|whitelist",
    re.I,
)
NEGATED_BAN = re.compile(
    r"不(?:禁止|限制|反对|反對)(?:发布|發佈|发送|發送)?(?:广告|廣告|推广|推廣)"
)
TOPIC_CONDITION = re.compile(
    r"(?:仅|只|限|指定).{0,12}(?:话题|話題|广告区|廣告區)|(?:topic|thread).{0,20}(?:only|required)",
    re.I,
)
LINK_BAN = re.compile(
    r"(?:禁|不许|不允许|不得|勿).{0,12}(?:外链|外鏈|链接|鏈接|拉群|引流)|(?:no|ban).{0,10}(?:links|invites)",
    re.I,
)


def naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def text_of(message: Any) -> str:
    return str(getattr(message, "message", None) or getattr(message, "text", None) or "").strip()


def content_key(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", "", text).casefold().encode()).hexdigest()


def promotion_targets(text: str) -> set[str]:
    """Compare destinations and contact handles independently of tracking parameters."""
    targets: set[str] = set()
    for url in URL.findall(text):
        url = url.rstrip("/.,，。!！)）]")
        parsed = urlsplit(url if "://" in url else "https://" + url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        path = parsed.path.rstrip("/")
        if host in {"t.me", "telegram.me", "telegram.dog"}:
            targets.add("tg:" + path.lstrip("/").split("/")[0].lower())
        elif host:
            targets.add(host + path)
    for match in CONTACT.finditer(text):
        targets.add(
            ("tg:" + match.group(1).lower())
            if match.group(1)
            else ("contact:" + match.group(2).lower())
        )
    return targets


def promotion_key(text: str) -> str:
    targets = promotion_targets(text)
    return "|".join(sorted(targets)) if targets else content_key(text)


def looks_like_ad(text: str) -> bool:
    if len(text) < 12 or text.startswith((">", "请问", "求助", "有没有", "有人知道")):
        return False
    return bool(OFFER.search(text) and CTA.search(text))


def possible_rule(text: str) -> bool:
    """Treat ad-like text as a possible rule only when it contains a directive."""
    return bool(
        WARNING.search(text)
        or RULE_DIRECTIVE.search(text)
        or (RULE.search(text) and not looks_like_ad(text))
    )


def evidence_age(message: Any, now: datetime) -> float | None:
    dates = [naive(getattr(message, name, None)) for name in ("date", "edit_date")]
    dates = [item for item in dates if item is not None]
    return max(0.0, (naive(now) - max(dates)).total_seconds() / 3600) if dates else None


def trial_proof(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One verified ordinary member's ad retained with unchanged content for a full day."""
    eligible = [
        item
        for item in evidence
        if item.get("source") == "recent_promotional_message"
        and item.get("sender_role") == "ordinary"
        and type(item.get("sender_id")) is int
        and item["sender_id"] > 0
        and type(item.get("message_id")) is int
        and item["message_id"] > 0
        and not item.get("system_account")
        and item.get("accessible") is True
        and item.get("warning_search_complete") is True
        and not item.get("warning_reply_ids")
        and float(item.get("age_hours") or 0) >= 24
        and looks_like_ad(str(item.get("text") or ""))
    ]
    return eligible[:1]

def trial_evidence(evidence: list[dict[str, Any]]) -> bool:
    return bool(trial_proof(evidence))


def select_advertising_evidence(adverts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Retain ordinary-member proof before the cap, without losing dedup bridges."""
    parents = list(range(len(adverts)))

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    owners: dict[tuple[str, str], int] = {}
    for index, advert in enumerate(adverts):
        keys = [("text", content_key(advert["text"]))]
        keys.extend(("target", target) for target in advert.get("promotion_targets", []))
        if advert.get("association_key"):
            keys.append(("association", advert["association_key"]))
        for key in keys:
            if key in owners:
                parents[root(index)] = root(owners[key])
            else:
                owners[key] = index
    cluster_keys: dict[int, list[str]] = {}
    for key, index in owners.items():
        cluster_keys.setdefault(root(index), []).append(":".join(key))
    identities = {
        cluster: hashlib.sha256("|".join(sorted(keys)).encode()).hexdigest()
        for cluster, keys in cluster_keys.items()
    }
    for index, advert in enumerate(adverts):
        # Keep transitive shared contacts even when a bridging duplicate is omitted.
        advert["association_key"] = "sample_campaign:" + identities[root(index)]

    def priority(index: int) -> tuple:
        advert = adverts[index]
        role = advert.get("sender_role")
        rank = (
            0
            if role == "ordinary" and not advert.get("warning_reply_ids")
            else 1
            if role == "ordinary"
            else 2
            if role == "admin"
            else 3
            if role in {"unknown", "anonymous"}
            else 4
        )
        return (rank, -(float(advert.get("age_hours") or 0)), index)

    ordered = sorted(range(len(adverts)), key=priority)
    eligible = [
        index
        for index in ordered
        if adverts[index].get("sender_role") == "ordinary"
        and isinstance(adverts[index].get("sender_id"), int)
        and adverts[index]["sender_id"] > 0
        and not adverts[index].get("system_account")
        and adverts[index].get("accessible") is not False
        and not adverts[index].get("warning_reply_ids")
    ]
    # Reserve an existing valid A/B pair before choosing one representative per
    # campaign. Picking only one sender first could accidentally erase that pair.
    anchors = []
    for position, first in enumerate(eligible):
        if float(adverts[first].get("age_hours") or 0) < 24:
            continue
        for second in eligible[position + 1 :]:
            if (
                root(first) != root(second)
                and float(adverts[second].get("age_hours") or 0) >= 24
                and adverts[first]["sender_id"] != adverts[second]["sender_id"]
                and adverts[first].get("topic_id") == adverts[second].get("topic_id")
            ):
                anchors = [first, second]
                break
        if anchors:
            break
    selected, seen = [], set()
    for index in [*anchors, *ordered]:
        advert = adverts[index]
        key = (root(index), advert.get("sender_role"), advert.get("topic_id"))
        if key in seen:
            continue
        seen.add(key)
        selected.append(advert)
    return selected[:MAX_AD_EVIDENCE], len(selected)


def quality_decision(snapshot: dict[str, Any]) -> tuple[str, str]:
    """Incomplete evidence must never manufacture a negative fact."""
    if snapshot.get("protected"):
        return "protected", "protected_membership"
    if snapshot.get("technical_errors"):
        return "technical_wait", "evidence_read_failed"
    permission = snapshot.get("permissions", {})
    if permission.get("member") is False:
        return "technical_wait", "membership_not_participant"
    permission = snapshot.get("permissions", {})
    if permission.get("member") is not True:
        return "technical_wait", "membership_unconfirmed"
    if permission.get("temporary_until") or permission.get("slowmode_until"):
        return "wait", "temporary_send_restriction"
    if permission.get("can_send_text") is False:
        return "observe", "verification_or_send_restriction"
    if permission.get("can_send_text") is not True:
        return "technical_wait", "permission_unknown"
    if permission.get("paid_messages"):
        return "observe", "paid_messages_not_authorized"
    if snapshot.get("rules_incomplete") and not trial_proof(
        [item for item in snapshot.get("evidence", []) if item.get("topic_id") is None]
    ):
        return "observe", "rules_coverage_incomplete"
    return "qualified", "quality_and_permissions_passed"


def _flood_wait(exc: Exception) -> bool:
    from app.core.account.rpc_governor import RpcDeferred
    if isinstance(exc, RpcDeferred):
        return True
    name = type(exc).__name__
    return "Flood" in name or "SlowModeWait" in name


def _message_metadata(message: Any, now: datetime) -> dict[str, Any]:
    reply = getattr(message, "reply_to", None)
    forward = getattr(message, "fwd_from", None)
    date, edited = naive(getattr(message, "date", None)), naive(getattr(message, "edit_date", None))
    forward_peer = getattr(forward, "from_id", None)
    peer_id = next(
        (
            getattr(forward_peer, key, None)
            for key in ("user_id", "channel_id", "chat_id")
            if getattr(forward_peer, key, None) is not None
        ),
        None,
    )
    return {
        "message_id": getattr(message, "id", None),
        "sender_id": getattr(message, "sender_id", None),
        "date": date.isoformat() if date else None,
        "edited_at": edited.isoformat() if edited else None,
        "age_hours": evidence_age(message, now),
        "topic_id": (
            getattr(reply, "reply_to_top_id", None)
            or (
                getattr(reply, "reply_to_msg_id", None)
                if getattr(reply, "forum_topic", False)
                else None
            )
        ),
        "reply_to_message_id": getattr(reply, "reply_to_msg_id", None),
        "forward_origin": {
            "peer_type": type(forward_peer).__name__ if forward_peer is not None else None,
            "peer_id": peer_id,
            "message_id": getattr(forward, "channel_post", None),
            "date": naive(forward.date).isoformat() if getattr(forward, "date", None) else None,
            "hidden_origin": bool(getattr(forward, "from_name", None)),
        }
        if forward is not None
        else None,
        "has_media": bool(getattr(message, "media", None)),
        "accessible": True,
        "observed_at": now.isoformat(),
    }


def append_rule(
    result: dict[str, Any],
    source: str,
    text: str,
    now: datetime,
    message: Any = None,
    role: str = "group_metadata",
) -> None:
    if text:
        result["evidence"].append(
            {
                "source": source,
                "text": text,
                "sender_role": role,
                "scope": "group" if message is None else "message_context_requires_review",
                **(
                    _message_metadata(message, now)
                    if message is not None
                    else {
                        "message_id": None,
                        "date": None,
                        "edited_at": None,
                        "observed_at": now.isoformat(),
                    }
                ),
            }
        )


class EvidenceCollector:
    """Uses an already leased AccountPool client; never sends, joins or leaves."""

    def __init__(self, client: Any, *, own_user_ids: set[int] | None = None) -> None:
        self.client = client
        self.own_user_ids = own_user_ids or set()
        self.roles: dict[int, str] = {}
        self.role_errors: dict[int, str] = {}
        self.permission_queries = 0
        self.sender_resolution_queries = 0
        self.previous: dict[str, Any] = {}
        self.collection_now = datetime.utcnow()
        self.message_senders: dict[int, int] = {}
        self.progress_attempted_ids: set[int] = set()
        self.permission_failures: dict[str, dict[str, Any]] = {}
        self.permission_denials = 0

    async def _resume_messages(self, entity: Any, result: dict[str, Any], now: datetime) -> list[Any]:
        """Resume by message ID, always fetching the current message and role."""
        anchors = [
            item["message_id"]
            for item in self.previous.get("evidence", [])
            if item.get("source") == "recent_promotional_message"
            and item.get("sender_role") == "ordinary"
            and not item.get("system_account")
            and not item.get("topic_id")
            and item.get("accessible") is True
            and not item.get("warning_reply_ids")
            and type(item.get("message_id")) is int
            and item["message_id"] > 0
        ][:6]
        pending = (self.previous.get("collection_progress") or {}).get("pending_message_ids", [])
        ids = list(dict.fromkeys([*anchors, *[value for value in pending if type(value) is int and value > 0]]))[:12]
        if not ids:
            return []
        try:
            async with asyncio.timeout(15):
                messages = await self.client.get_messages(entity, ids=ids)
            self.progress_attempted_ids.update(ids)
            result["targeted_progress_checked"] = len(ids)
            return [
                item
                for item in (messages or [])
                if item is not None
                and getattr(item, "id", None) in ids
                and naive(getattr(item, "date", None)) is not None
                and now - timedelta(hours=WINDOW_HOURS) <= naive(item.date) <= now
            ]
        except Exception as exc:
            self._error(result, "progress_read", exc)
            return []

    async def role(self, entity: Any, message: Any) -> str:
        sender_id = getattr(message, "sender_id", None)
        if not isinstance(sender_id, int) or sender_id <= 0:
            return "anonymous"
        if sender_id in self.own_user_ids:
            return "system"
        message_id = getattr(message, "id", None)
        if type(message_id) is int and message_id > 0:
            self.message_senders[message_id] = sender_id
        priority_rule = possible_rule(text_of(message))
        identity_limit = MAX_IDENTITIES + (MAX_PRIORITY_IDENTITIES if priority_rule else 0)
        sender = getattr(message, "sender", None)
        # Telegram already supplied this identity with the message. Known bots
        # need no participant RPC and must not consume the human lookup budget.
        if sender is not None and getattr(sender, "bot", False):
            self.roles[sender_id] = "bot"
            self.role_errors.pop(sender_id, None)
            return "bot"
        cached = self.roles.get(sender_id)
        if cached is not None and (
            cached != "unknown"
            or not priority_rule
            or self.role_errors.get(sender_id)
            not in {"identity_budget_exhausted", "sender_resolution_budget_exhausted"}
        ):
            return cached
        self.roles[sender_id] = "unknown"
        from app.modules.acquisition.evidence_progress import date
        prior_failure = self.permission_failures.get(str(sender_id), {})
        retry_at = date(prior_failure.get("retry_at"))
        if retry_at and retry_at > self.collection_now:
            self.role_errors[sender_id] = "ChatAdminRequiredError"
            return "unknown"
        if self.permission_denials >= 3:
            self.role_errors[sender_id] = "permission_probe_budget_exhausted"
            return "unknown"
        try:
            if sender is None:
                get_sender = getattr(message, "get_sender", None)
                if callable(get_sender):
                    if self.sender_resolution_queries >= identity_limit:
                        self.role_errors[sender_id] = "sender_resolution_budget_exhausted"
                        return "unknown"
                    self.sender_resolution_queries += 1
                    sender = await get_sender()
                if sender is None:
                    if self.sender_resolution_queries >= identity_limit:
                        self.role_errors[sender_id] = "sender_resolution_budget_exhausted"
                        return "unknown"
                    self.sender_resolution_queries += 1
                    sender = await self.client.get_entity(sender_id)
            if sender is None:
                self.role_errors[sender_id] = "sender_unavailable"
                return "unknown"
            if getattr(sender, "bot", False):
                self.roles[sender_id] = "bot"
                self.role_errors.pop(sender_id, None)
                return "bot"
            if getattr(sender, "deleted", False):
                self.role_errors[sender_id] = "sender_deleted"
                return "unknown"
            if self.permission_queries >= identity_limit:
                self.role_errors[sender_id] = "identity_budget_exhausted"
                return "unknown"
            self.permission_queries += 1
            permission = await self.client.get_permissions(entity, sender)
            if permission is None or not hasattr(permission, "is_admin"):
                raise ValueError("participant_permission_unavailable")
            participant = getattr(permission, "participant", None)
            role = (
                "admin"
                if getattr(permission, "is_admin", False)
                or getattr(permission, "is_creator", False)
                else "ordinary"
            )
            if (
                getattr(permission, "has_left", False)
                or getattr(permission, "is_banned", False)
                or type(participant).__name__.endswith(("Left", "Banned"))
            ):
                role = "unknown"
            self.roles[sender_id] = role
            if role != "unknown":
                self.role_errors.pop(sender_id, None)
                self.permission_failures.pop(str(sender_id), None)
        except Exception as exc:
            self.role_errors[sender_id] = type(exc).__name__
            if type(exc).__name__ == "ChatAdminRequiredError":
                self.permission_denials += 1
                failures = min(4, int(prior_failure.get("attempts") or 0) + 1)
                self.permission_failures[str(sender_id)] = {
                    "attempts": failures,
                    "retry_at": (self.collection_now + timedelta(hours=min(12, 2 ** failures))).isoformat(),
                }
            if _flood_wait(exc):
                raise
        return self.roles[sender_id]

    async def messages(self, entity: Any, **kwargs: Any) -> list[Any]:
        return [item async for item in self.client.iter_messages(entity, **kwargs)]

    def _error(self, result: dict[str, Any], source: str, exc: Exception) -> bool:
        from app.core.account.rpc_governor import RpcDeferred
        if isinstance(exc, RpcDeferred):
            result["collection_halted"] = exc.reason
            result["local_rpc_defer_reason"] = exc.reason
            result["retry_after_seconds"] = exc.retry_after_seconds
            return True
        result["technical_errors"].append(source + ":" + type(exc).__name__)
        if _flood_wait(exc):
            result["collection_halted"] = "telegram_rate_limit"
            result["retry_after_seconds"] = max(1, int(getattr(exc, "seconds", 0) or 60))
            return True
        return False

    def _finish(self, result: dict[str, Any]) -> dict[str, Any]:
        old_pending = (self.previous.get("collection_progress") or {}).get("pending_message_ids", [])
        pending = [
            value for value in old_pending
            if type(value) is int and value > 0 and value not in self.progress_attempted_ids
            and value not in self.message_senders
        ]
        budget_errors = {"identity_budget_exhausted", "sender_resolution_budget_exhausted"}
        pending_senders = set()
        for message_id, sender_id in self.message_senders.items():
            if self.role_errors.get(sender_id) in budget_errors and sender_id not in pending_senders:
                pending.append(message_id)
                pending_senders.add(sender_id)
        result["collection_progress"] = {
            "version": 1,
            "pending_message_ids": list(dict.fromkeys(pending))[:100],
            "permission_failures": dict(list(self.permission_failures.items())[-100:]),
        }
        result["roles"] = {str(key): value for key, value in self.roles.items()}
        result["identity_errors"] = {str(key): value for key, value in self.role_errors.items()}
        result["permission_queries"] = self.permission_queries
        result["sender_resolution_queries"] = self.sender_resolution_queries
        result["unknowns"] = sorted(set(result["unknowns"]))
        result["technical_errors"] = sorted(set(result["technical_errors"]))
        result["trial_history_sufficient"] = trial_evidence(result["evidence"])
        result["root_ad_history_sufficient"] = trial_evidence(
            [item for item in result["evidence"] if item.get("topic_id") is None]
        )
        result["quality_status"], result["quality_reason"] = quality_decision(result)
        return result

    async def _history(
        self, entity: Any, result: dict[str, Any], now: datetime, hidden: bool
    ) -> list[Any]:
        """First sample all three days; then fill each unfinished segment within one cap."""
        segments = [
            {
                "start": now - timedelta(hours=24 * (index + 1)),
                "end": now - timedelta(hours=24 * index),
                "offset_id": 0,
                "sample_count": 0,
                "complete": False,
                "attempted": False,
                "observed_oldest": None,
                "observed_newest": None,
            }
            for index in range(3)
        ]
        messages: dict[int, Any] = {}
        total = 0

        async def read_segment(segment: dict[str, Any], limit: int) -> None:
            nonlocal total
            segment["attempted"] = True
            batch = []
            # Count each observed item before the iterator can fail mid-page.
            async for item in self.client.iter_messages(
                entity,
                limit=limit,
                offset_id=segment["offset_id"],
                offset_date=(segment["end"] + timedelta(microseconds=1)).replace(tzinfo=UTC),
            ):
                batch.append(item)
                total += 1
                segment["sample_count"] += 1
                if len(batch) >= limit:
                    break
            if not batch:
                segment["complete"] = True
                return
            previous_offset = segment["offset_id"]
            for message in batch:
                date = naive(getattr(message, "date", None))
                if date is None:
                    result["unknowns"].append("message_date_unknown")
                    continue
                if date < segment["start"]:
                    segment["complete"] = True
                    break
                if date <= segment["end"]:
                    message_id = getattr(message, "id", None)
                    if message_id is None:
                        result["unknowns"].append("message_id_unknown")
                    else:
                        messages[message_id] = message
                    segment["observed_oldest"] = min(segment["observed_oldest"] or date, date)
                    segment["observed_newest"] = max(segment["observed_newest"] or date, date)
            if len(batch) < limit:
                segment["complete"] = True
            segment["offset_id"] = int(getattr(batch[-1], "id", 0) or 0)
            if not segment["complete"] and (
                segment["offset_id"] == previous_offset or not segment["offset_id"]
            ):
                segment["stalled"] = True
                result["unknowns"].append("history_pagination_stalled")

        halted = False
        # Each round grants at most 100 per day. The first round is capped at 300.
        while total < MAX_MESSAGES:
            pending = [
                segment
                for segment in segments
                if not segment["complete"] and not segment.get("stalled")
            ]
            if not pending:
                break
            for segment in pending:
                if total >= MAX_MESSAGES:
                    break
                try:
                    await read_segment(segment, min(100, MAX_MESSAGES - total))
                except Exception as exc:
                    segment["error"] = type(exc).__name__
                    segment["stalled"] = True
                    if self._error(result, "history", exc):
                        halted = True
                        break
            if halted:
                break
            if total < MAX_MESSAGES and all(segment["attempted"] for segment in segments):
                probe = await self._classify_history(
                    entity, copy.deepcopy(result), list(messages.values()), now
                )
                missing = []
                if not probe.get("trial_history_sufficient"):
                    missing.append("ordinary_member_advertising")
                sampled_ids = set(messages)
                if any(
                    WARNING.search(text_of(message))
                    and getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None)
                    and message.reply_to.reply_to_msg_id not in sampled_ids
                    for message in messages.values()
                ):
                    missing.append("warning_target_not_sampled")
                if probe.get("rules_incomplete") or probe.get("technical_errors"):
                    missing.append("rules_or_identity")
                result["sampling_missing_facts"] = missing
                if probe.get("collection_halted"):
                    result.update(
                        {
                            key: probe[key]
                            for key in (
                                "collection_halted",
                                "retry_after_seconds",
                                "technical_errors",
                            )
                            if key in probe
                        }
                    )
                    break
                if not missing:
                    result["sampling_stopped_reason"] = "ordinary_member_advertising_found"
                    break
        coverage = []
        for segment in segments:
            complete = segment["complete"] and not hidden and not segment.get("error")
            coverage.append(
                {
                    "window_start": segment["start"].isoformat(),
                    "window_end": segment["end"].isoformat(),
                    "complete": complete,
                    "sample_count": segment["sample_count"],
                    "attempted": segment["attempted"],
                    "observed_oldest": segment["observed_oldest"].isoformat()
                    if segment["observed_oldest"]
                    else None,
                    "observed_newest": segment["observed_newest"].isoformat()
                    if segment["observed_newest"]
                    else None,
                    "unknown_reason": (
                        "history_hidden"
                        if hidden
                        else segment.get("error")
                        or (
                            "pagination_stalled"
                            if segment.get("stalled")
                            else (
                                "sample_limit"
                                if not complete and total >= MAX_MESSAGES
                                else (
                                    (
                                        "minimum_evidence_sampled"
                                        if result.get("sampling_stopped_reason")
                                        else "collection_halted"
                                    )
                                    if not complete
                                    else None
                                )
                            )
                        )
                    ),
                }
            )
        result["coverage"] = coverage
        result["history_complete"] = all(item["complete"] for item in coverage)
        result["sample_count"] = total
        result["unique_sample_count"] = len(messages)
        result["sample_limit_reached"] = total >= MAX_MESSAGES
        if hidden:
            result["unknowns"].append("history_hidden")
        if not result["history_complete"]:
            result["unknowns"].append("history_coverage_incomplete")
        dates = [naive(message.date) for message in messages.values()]
        result["observed_oldest"] = min(dates).isoformat() if dates else None
        result["observed_newest"] = max(dates).isoformat() if dates else None
        return list(messages.values())

    async def collect(self, entity: Any, *, now: datetime | None = None) -> dict[str, Any]:
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.functions.messages import GetFullChatRequest, GetOnlinesRequest
        from telethon.tl.types import InputMessagesFilterPinned

        now = naive(now) or datetime.utcnow()
        self.collection_now = now
        from app.modules.acquisition.evidence_progress import date
        prior_date = date(self.previous.get("collected_at")) or date(self.previous.get("checked_at"))
        if prior_date is None or not now - timedelta(hours=24) <= prior_date <= now:
            self.previous = {}
        self.permission_failures = {
            str(key): dict(value)
            for key, value in ((self.previous.get("collection_progress") or {}).get("permission_failures") or {}).items()
            if isinstance(value, dict) and str(key).isdigit()
            and date(value.get("retry_at")) is not None
            and date(value.get("retry_at")) <= now + timedelta(hours=12)
        }
        cutoff = now - timedelta(hours=WINDOW_HOURS)
        result: dict[str, Any] = {
            "policy_version": POLICY_VERSION,
            "checked_at": now.isoformat(),
            "window_start": cutoff.isoformat(),
            "window_end": now.isoformat(),
            "member_count": None,
            "member_count_source": "unknown",
            "member_count_checked_at": now.isoformat(),
            "member_count_verified": False,
            "online_count": None,
            "online_count_source": "unknown",
            "online_count_checked_at": now.isoformat(),
            "valid_messages": 0,
            "sample_count": 0,
            "history_complete": False,
            "rules_incomplete": False,
            "protected": False,
            "permissions": {},
            "technical_errors": [],
            "unknowns": [],
            "evidence": [],
            "roles": [],
            "coverage": [],
            "title": getattr(entity, "title", ""),
            "raw_peer_id": getattr(entity, "id", None),
            "group_type": (
                "supergroup"
                if getattr(entity, "megagroup", False)
                else "channel"
                if getattr(entity, "broadcast", False)
                else "basic_group"
            ),
            "migrated_to": getattr(getattr(entity, "migrated_to", None), "channel_id", None),
        }
        full = None
        try:
            request = (
                GetFullChannelRequest(entity)
                if hasattr(entity, "megagroup")
                else GetFullChatRequest(entity.id)
            )
            response = await self.client(request)
            full = response.full_chat
            count = getattr(full, "participants_count", None)
            if count is not None:
                result["member_count_source"] = "full_chat.participants_count"
            else:
                participants = getattr(getattr(full, "participants", None), "participants", None)
                if participants is not None:
                    count = len(participants)
                    result["member_count_source"] = "full_chat.participants"
                else:
                    current_chat = next(
                        (
                            chat
                            for chat in getattr(response, "chats", [])
                            if getattr(chat, "id", None) == getattr(entity, "id", None)
                        ),
                        None,
                    )
                    count = getattr(current_chat, "participants_count", None)
                    if count is not None:
                        result["member_count_source"] = "full_response.chat.participants_count"
            result["member_count"] = count
            result["member_count_verified"] = count is not None
            online_count = getattr(full, "online_count", None)
            if isinstance(online_count, int) and online_count >= 0:
                result["online_count"] = online_count
                result["online_count_source"] = "full_chat.online_count"
            result["cached_member_count"] = getattr(entity, "participants_count", None)
            result["auto_delete_seconds"] = getattr(full, "ttl_period", None)
            result["migrated_from_chat_id"] = getattr(full, "migrated_from_chat_id", None)
        except Exception as exc:
            if self._error(result, "full_info", exc):
                return self._finish(result)

        if result["online_count"] is None:
            try:
                online_result = await self.client(GetOnlinesRequest(entity))
                online_count = getattr(online_result, "onlines", None)
                if isinstance(online_count, int) and online_count >= 0:
                    result["online_count"] = online_count
                    result["online_count_source"] = "messages.getOnlines"
                else:
                    result["unknowns"].append("online_count_unavailable")
            except Exception as exc:
                result["unknowns"].append("online_count_" + type(exc).__name__)
                if _flood_wait(exc) and self._error(result, "online_count", exc):
                    return self._finish(result)

        rights = getattr(entity, "default_banned_rights", None)
        permissions: dict[str, Any] = {"member": None, "can_send_text": None}
        try:
            me = await self.client.get_permissions(entity, "me")
            if me is None or not hasattr(me, "is_admin"):
                raise ValueError("self_permission_unavailable")
            participant = getattr(me, "participant", None)
            is_admin = bool(getattr(me, "is_admin", False) or getattr(me, "is_creator", False))
            result["protected"] = is_admin
            permissions["is_admin"] = is_admin
            permissions["is_creator"] = bool(getattr(me, "is_creator", False))
            own = getattr(participant, "banned_rights", None)
            permissions["member"] = not (
                getattr(me, "has_left", False)
                or getattr(me, "is_banned", False)
                or type(participant).__name__.endswith("Left")
                or getattr(own, "view_messages", False)
                or getattr(entity, "left", False)
            )
            permissions["membership_state"] = "member" if permissions["member"] else "not_member"
            permissions["membership_evidence"] = "telegram_permissions" if (
                getattr(me, "has_left", False)
                or getattr(me, "is_banned", False)
                or type(participant).__name__.endswith("Left")
                or getattr(own, "view_messages", False)
            ) else None
            for name, fields in {
                "can_send_text": ("send_messages", "send_plain"),
                "can_send_photo": ("send_messages", "send_media", "send_photos"),
                "can_send_video": ("send_messages", "send_media", "send_videos"),
                "can_send_file": ("send_messages", "send_media", "send_docs"),
                "can_send_audio": ("send_messages", "send_media", "send_audios"),
                "can_send_voice": ("send_messages", "send_media", "send_voices"),
                "can_send_poll": ("send_messages", "send_polls"),
                "can_preview_links": ("send_messages", "embed_links"),
            }.items():
                permissions[name] = bool(
                    permissions["member"]
                    and (
                        is_admin
                        or not any(
                            getattr(value, field, False)
                            for value in (rights, own)
                            for field in fields
                        )
                    )
                )
            # Telegram's embed_links controls previews, not literal URL text.
            permissions["can_send_url_text"] = permissions["can_send_text"]
            permissions["url_content_rules_checked"] = False
            blocked_rights = [value for value in (rights, own) if value is not None
                              and any(getattr(value, key, False) for key in ("send_messages", "send_plain"))]
            from app.modules.acquisition.send_restriction import restriction_facts
            permissions.update(restriction_facts(blocked_rights, now))
            permissions["permanent_send_restriction_verified"] = bool(
                permissions["member"] and not permissions["can_send_text"]
                and permissions["permanent_send_restriction_verified"])
            permissions["restriction_kind"] = (
                "temporary"
                if permissions.get("temporary_until")
                else "indefinite"
                if permissions["member"] and not permissions["can_send_text"]
                else "none"
            )
        except Exception as exc:
            if type(exc).__name__ == "UserNotParticipantError":
                permissions.update(
                    member=False,
                    membership_state="not_member",
                    membership_evidence="telegram_permissions_not_participant",
                )
                result["permissions"] = permissions
                return self._finish(result)
            result["permissions"] = permissions
            if self._error(result, "self_permissions", exc):
                return self._finish(result)
        slow = getattr(full, "slowmode_next_send_date", None)
        if isinstance(slow, int):
            slow = datetime.fromtimestamp(slow, UTC)
        if isinstance(slow, datetime) and naive(slow) > now:
            permissions["slowmode_until"] = naive(slow).isoformat()
        permissions["slowmode_seconds"] = getattr(full, "slowmode_seconds", None)
        permissions["paid_messages"] = int(getattr(full, "send_paid_messages_stars", 0) or 0) > 0
        permissions["forum"] = bool(getattr(entity, "forum", False))
        result["permissions"] = permissions
        if (getattr(self, "stop_on_permanent_mute", False)
                and permissions.get("permanent_send_restriction_verified")
                and not permissions.get("temporary_until") and not permissions.get("slowmode_until")
                and not result.get("technical_errors")):
            result["sampling_stopped_reason"] = "confirmed_long_term_send_restriction"
            return self._finish(result)

        def add_rule(
            source: str, text: str, message: Any = None, role: str = "group_metadata"
        ) -> None:
            if not text:
                return
            result["evidence"].append(
                {
                    "source": source,
                    "text": text,
                    "sender_role": role,
                    "scope": "group" if message is None else "message_context_requires_review",
                    **(
                        _message_metadata(message, now)
                        if message is not None
                        else {
                            "message_id": None,
                            "date": None,
                            "edited_at": None,
                            "observed_at": now.isoformat(),
                        }
                    ),
                }
            )

        add_rule("full_about", str(getattr(full, "about", "") or ""))
        resumed = await self._resume_messages(entity, result, now)
        if result.get("collection_halted"):
            return self._finish(result)
        # Reserve the first identity reads for the unfinished work. This is
        # fresh evidence; old roles and old visibility are never copied.
        for message in resumed:
            try:
                await self.role(entity, message)
            except Exception as exc:
                if self._error(result, "progress_identity", exc):
                    return self._finish(result)
        try:
            pinned = await self.messages(
                entity, filter=InputMessagesFilterPinned(), limit=MAX_PINS + 1
            )
            result["pinned_sample_count"] = min(len(pinned), MAX_PINS)
            if len(pinned) > MAX_PINS:
                result["rules_incomplete"] = True
                result["unknowns"].append("pinned_history_limit")
            for message in pinned[:MAX_PINS]:
                role = await self.role(entity, message)
                add_rule("pinned_message", text_of(message), message, role)
                if not text_of(message) and getattr(message, "media", None):
                    result["rules_incomplete"] = True
                    result["unknowns"].append("pinned_media_unreadable")
        except Exception as exc:
            result["rules_incomplete"] = True
            if self._error(result, "pinned", exc):
                return self._finish(result)

        hidden = bool(
            getattr(full, "hidden_prehistory", False) or getattr(full, "available_min_id", None)
        )
        if resumed:
            probe = await self._classify_history(entity, copy.deepcopy(result), resumed, now, verify_threads=True)
            if probe.get("collection_halted"):
                return probe
            if probe.get("root_ad_history_sufficient"):
                probe["sampling_stopped_reason"] = "resumed_live_ordinary_ad_verified"
                probe["history_complete"] = False
                probe["unknowns"].append("history_coverage_incomplete")
                return self._finish(probe)
        all_messages = await self._history(entity, result, now, hidden)
        if result.get("collection_halted"):
            return self._finish(result)
        combined = {message.id: message for message in [*resumed, *all_messages]}
        return await self._classify_history(entity, result, list(combined.values()), now, verify_threads=True)

    async def _verify_warning_thread(
        self, entity: Any, advert: dict[str, Any], now: datetime, result: dict[str, Any]
    ) -> bool:
        """Check one ad's complete reply thread when 72-hour history is capped.

        Returns True only when a Telegram rate limit requires stopping collection.
        An unavailable or oversized thread stays unknown and never becomes proof.
        """
        message_id = advert.get("message_id")
        if type(message_id) is not int or message_id <= 0:
            return False
        try:
            async with asyncio.timeout(15):
                root = await self.client.get_messages(entity, ids=message_id)
                if root is None:
                    advert["accessible"] = False
                    result["unknowns"].append("advertisement_no_longer_accessible")
                    return False
                if content_key(text_of(root)) != content_key(advert["text"]):
                    result["unknowns"].append("advertisement_content_changed")
                    return False
                age = evidence_age(root, now)
                advert["age_hours"] = age
                if age is None or age < 24:
                    return False
                reply_info = getattr(root, "replies", None)
                declared = getattr(reply_info, "replies", None)
                if type(declared) is not int or not 0 <= declared <= MAX_WARNING_THREAD_REPLIES:
                    result["unknowns"].append("warning_reply_count_unconfirmed")
                    return False
                replies = await self.messages(
                    entity, reply_to=message_id, limit=MAX_WARNING_THREAD_REPLIES + 1
                )
            if len(replies) != declared:
                result["unknowns"].append("warning_reply_coverage_incomplete")
                return False
            advert["warning_reply_ids"] = [
                reply.id for reply in replies if WARNING.search(text_of(reply))
            ]
            advert["warning_search_complete"] = True
            advert["warning_search_scope"] = "complete_reply_thread"
        except Exception as exc:
            if _flood_wait(exc):
                return self._error(result, "warning_reply_thread", exc)
            result["unknowns"].append("warning_reply_query_unavailable")
            advert["warning_reply_error"] = type(exc).__name__
        return False

    async def _classify_history(
        self,
        entity: Any,
        result: dict[str, Any],
        all_messages: list[Any],
        now: datetime,
        *,
        verify_threads: bool = False,
    ) -> dict[str, Any]:
        cutoff = now - timedelta(hours=WINDOW_HOURS)
        seen_text: set[str] = set()
        seen_targets: set[str] = set()
        adverts: list[dict[str, Any]] = []
        feedback: list[dict[str, Any]] = []
        # Check potential negative rules first; cached bot identity never uses
        # the separate participant lookup budget.
        all_messages.sort(
            key=lambda message: (
                0
                if possible_rule(text_of(message))
                else 1
                if looks_like_ad(text_of(message)) and (evidence_age(message, now) or 0) >= 24
                else 2
                if looks_like_ad(text_of(message))
                else 3,
                -(naive(message.date) - cutoff).total_seconds(),
            )
        )
        for message in all_messages:
            date = naive(getattr(message, "date", None))
            if not date or not cutoff <= date <= now or getattr(message, "action", None):
                continue
            text = text_of(message)
            if not text:
                if getattr(message, "media", None):
                    result["unknowns"].append("media_without_readable_caption")
                continue
            try:
                role = await self.role(entity, message)
            except Exception as exc:
                if self._error(result, "sender_identity", exc):
                    return self._finish(result)
                role = "unknown"
            metadata = _message_metadata(message, now)
            is_feedback = bool(role in {"admin", "bot"} and WARNING.search(text))
            if is_feedback:
                item = {
                    "source": "moderation_feedback",
                    "text": text,
                    "sender_role": role,
                    "verified_admin": role == "admin",
                    "scope": "replied_message"
                    if metadata["reply_to_message_id"]
                    else "unspecified",
                    **metadata,
                }
                feedback.append(item)
                result["evidence"].append(item)
            is_rule = bool(
                role == "admin"
                and RULE.search(text)
                and not (is_feedback and metadata["reply_to_message_id"])
            )
            if is_rule:
                append_rule(result, "admin_rule", text, now, message, role)
            ad = looks_like_ad(text)
            text_key = content_key(text)
            targets = promotion_targets(text) if ad else set()
            duplicate = text_key in seen_text or bool(targets & seen_targets)
            if role in {"ordinary", "admin"} and not is_rule and not is_feedback and not duplicate:
                seen_text.add(text_key)
                seen_targets.update(targets)
                result["valid_messages"] += 1
            if role in {"unknown", "anonymous"}:
                result["unknowns"].append("sender_identity_unverified")
                if possible_rule(text):
                    # Unverified identity cannot turn a potential prohibition
                    # into missing evidence, nor be relabelled as an admin.
                    append_rule(result, "unverified_rule_message", text, now, message, role)
                    result["rules_incomplete"] = True
                    result["unknowns"].append("rule_sender_unverified")
            if ad:
                item = {
                    "source": "recent_promotional_message",
                    "text": text,
                    **metadata,
                    "sender_role": role,
                    "system_account": role == "system",
                    "classification": "marketing_candidate",
                    "promotion_key": promotion_key(text),
                    "promotion_targets": sorted(targets),
                    "warning_reply_ids": [],
                    "content_forms": ["text"]
                    + (["media"] if metadata["has_media"] else [])
                    + (["url_text"] if URL.search(text) else []),
                }
                adverts.append(item)
        for advert in adverts:
            advert["warning_reply_ids"] = [
                item["message_id"]
                for item in feedback
                if item["reply_to_message_id"] == advert["message_id"]
            ]
            advert["warning_search_complete"] = result["history_complete"] and not self.role_errors
        selected, deduplicated_count = select_advertising_evidence(adverts)
        result["advertising_candidates_by_role"] = {
            role: sum(item["sender_role"] == role for item in adverts)
            for role in sorted({item["sender_role"] for item in adverts})
        }
        # Exit decisions use the undeduplicated recent window. Trial proof has
        # a separate 24-hour retention requirement.
        result["ordinary_advertising_last_48h_count"] = sum(
            item["sender_role"] == "ordinary"
            and naive(datetime.fromisoformat(item["date"])) >= now - timedelta(hours=48)
            for item in adverts
            if item.get("date")
        )
        result["advertising_candidate_count"] = len(adverts)
        result["advertising_deduplicated_count"] = deduplicated_count
        result["advertising_retained_count"] = len(selected)
        if deduplicated_count > MAX_AD_EVIDENCE:
            result["unknowns"].append("advertising_evidence_limit")
        result["evidence"].extend(selected)
        if verify_threads:
            checked = 0
            for advert in selected:
                if (
                    advert.get("sender_role") != "ordinary"
                    or advert.get("system_account")
                    or advert.get("warning_reply_ids")
                    or advert.get("warning_search_complete") is True
                    or float(advert.get("age_hours") or 0) < 24
                ):
                    continue
                checked += 1
                if await self._verify_warning_thread(entity, advert, now, result):
                    return self._finish(result)
                if checked >= MAX_WARNING_THREAD_ADS:
                    break
            result["targeted_warning_checked_count"] = checked
        selected_ids = {item["message_id"] for item in selected}
        # The ordinary-ad cap must not discard the original content targeted by
        # a retained moderation warning. It is context, never trial proof.
        result["evidence"].extend(
            {**item, "source": "warned_promotional_message"}
            for item in adverts
            if item["warning_reply_ids"] and item["message_id"] not in selected_ids
        )
        return self._finish(result)


def snapshot_hash(snapshot: dict[str, Any], account_id: int, content_scope: str) -> str:
    value = {
        "version": POLICY_VERSION,
        "account_id": account_id,
        "scope": content_scope,
        "permissions": snapshot.get("permissions"),
        "evidence": snapshot.get("evidence"),
        "quality_status": snapshot.get("quality_status"),
        "member_count": snapshot.get("member_count"),
        "online_count": snapshot.get("online_count"),
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
