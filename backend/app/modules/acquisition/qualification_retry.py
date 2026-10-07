"""Stable rule-review retry identity, independent of unrelated ad traffic."""
from __future__ import annotations

import hashlib
import json


def rule_retry_fingerprint(snapshot: dict) -> str:
    sources = {"full_about", "pinned_message", "admin_rule", "unverified_rule_message",
               "moderation_feedback", "warned_promotional_message"}
    fields = ("source", "message_id", "text", "sender_role", "scope", "topic_id",
              "reply_to_message_id", "edited_at", "warning_reply_ids")
    material = [
        {key: item.get(key) for key in fields}
        for item in snapshot.get("evidence", []) if item.get("source") in sources
    ]
    material.sort(key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
    value = {"rules": material, "policy": snapshot.get("policy_version"),
             "profile": snapshot.get("profile_fingerprint")}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
