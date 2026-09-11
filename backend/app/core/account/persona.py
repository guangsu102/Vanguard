"""Account-level AI Persona domain contract.

This module deliberately has no Telegram, Redis, HTTP, or acquisition-ad
dependencies.  It owns canonical normalization and persistence mutations; the
API/execution callers own row-lock ordering, audit insertion, and commit.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

PERSONA_SCHEMA_VERSION = 1
PERSONA_MAX_TOTAL_BYTES = 16_384
PERSONA_PROMPT_TEMPLATE_VERSION = "owned-group-persona-v1"

ReplyLength = Literal["short", "medium", "long"]
AdStyle = Literal["neutral", "soft_share", "experience_share", "problem_solution"]
LanguageStyle = Literal["auto", "zh_cn"]


class PersonaSource(StrEnum):
    CONFIGURED = "configured"
    NEUTRAL_DEFAULT = "neutral_default"
    FEATURE_DISABLED_DEFAULT = "feature_disabled_default"
    LEGACY_DEFAULT = "legacy_default"
    DRAFT = "draft"


_ZERO_WIDTH_AND_BIDI_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]")
_ROLE_MARKER_RE = re.compile(
    r"(?im)(?:^|\n)\s*(?:#{1,6}\s*)?"
    r"(?:system|developer|assistant)\s*(?:message)?\s*[:：]"
    r"|<\|\s*(?:system|developer|assistant)\s*\|>"
    r"|\[\s*(?:system|developer|assistant)\s*\]"
    r"|<\/?\s*(?:system|developer|assistant)\s*>"
)
_ENGLISH_INJECTION_RE = re.compile(
    r"(?is)\b(?:ignore|disregard|override|bypass|disable|forget)\b"
    r".{0,80}\b(?:previous|system|developer|safety|moderation|guardrail|instruction|rule)s?\b"
    r"|\b(?:reveal|print|return|expose|steal)\b.{0,80}"
    r"\b(?:api[ _-]?key|secret|password|credential|token|system prompt)\b"
)
_CHINESE_INJECTION_RE = re.compile(
    r"(?:忽略|无视|绕过|覆盖|取消|关闭|规避).{0,40}"
    r"(?:系统|开发者|安全|治理|审核|风控|限制|规则|指令)"
    r"|(?:泄露|展示|输出|返回|告诉我|获取).{0,40}"
    r"(?:密钥|密码|口令|令牌|凭据|系统提示词|API\s*Key)",
    re.IGNORECASE | re.DOTALL,
)
_SECRET_REQUEST_RE = re.compile(
    r"(?is)\b(?:api[ _-]?key|secret|password|credential|access[ _-]?token)\b"
    r".{0,40}\b(?:show|give|send|reveal|print|return)\b"
)
_URL_RE = re.compile(
    r"(?i)(?:https?://|www\.|(?:t|telegram)\.me/|\b[a-z0-9][a-z0-9.-]+"
    r"\.(?:com|net|org|io|xyz|cn|co)(?:/|\b))"
)
_PRICE_RE = re.compile(
    r"(?i)(?:[$¥￥€£₽₿]\s*\d|\d(?:[\d,.]*\d)?\s*"
    r"(?:元|块|人民币|美元|美金|欧元|usd|usdt|cny|rmb|eur)(?:\b|$))"
)
_CONTACT_RE = re.compile(
    r"(?i)(?:\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b|"
    r"(?<!\w)@(?:[a-z][a-z0-9_]{4,31})(?!\w)|"
    r"\b(?:微信|wechat|wx|qq|telegram|电报|电话|手机|联系(?:我|我们)?)\s*[:：号]?|"
    r"(?<!\d)(?:\+?\d[\d\s-]{7,}\d)(?!\d))"
)
_CTA_RE = re.compile(
    r"(?i)(?:立即|马上|现在)?(?:点击|购买|下单|注册|订阅|扫码|领取|加入|进群|咨询)"
    r"|(?:私聊|联系|添加|加)我|click\s+(?:here|now)|buy\s+now|sign\s+up|join\s+us"
)

_LIST_ITEM_MAX_LENGTHS: Mapping[str, int] = {
    "interests": 40,
    "expertise": 40,
    "preferred_topics": 60,
    "forbidden_topics": 60,
    "catchphrases": 60,
}


def _normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = _ZERO_WIDTH_AND_BIDI_RE.sub("", value)
    value = "".join(
        character
        for character in value
        if character == "\n" or (ord(character) >= 32 and ord(character) != 127)
    )
    return value.strip()


def _contains_prompt_injection(value: str) -> bool:
    return bool(
        _ROLE_MARKER_RE.search(value)
        or _ENGLISH_INJECTION_RE.search(value)
        or _CHINESE_INJECTION_RE.search(value)
        or _SECRET_REQUEST_RE.search(value)
    )


def _canonical_json_from_payload(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class PersonaV1(BaseModel):
    """Validated and deeply immutable Persona v1 value object."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    name: str = Field(min_length=1, max_length=50)
    tone: str = Field(min_length=1, max_length=120)
    interests: tuple[str, ...] = Field(max_length=10)
    expertise: tuple[str, ...] = Field(max_length=10)
    reply_length: ReplyLength
    preferred_topics: tuple[str, ...] = Field(max_length=12)
    forbidden_topics: tuple[str, ...] = Field(max_length=20)
    ad_style: AdStyle
    catchphrases: tuple[str, ...] = Field(max_length=8)
    language_style: LanguageStyle
    system_prompt: str = Field(max_length=1000)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _validate_schema_version_type(cls, value: Any) -> Any:
        if type(value) is not int:
            raise PydanticCustomError(
                "persona_schema_version_invalid",
                "schema_version must be integer 1",
            )
        return value

    @field_validator(
        "name",
        "tone",
        "reply_length",
        "ad_style",
        "language_style",
        "system_prompt",
        mode="before",
    )
    @classmethod
    def _normalize_scalar(cls, value: Any) -> Any:
        return _normalize_text(value) if isinstance(value, str) else value

    @field_validator(
        "interests",
        "expertise",
        "preferred_topics",
        "forbidden_topics",
        "catchphrases",
        mode="before",
    )
    @classmethod
    def _normalize_list(cls, value: Any) -> Any:
        if not isinstance(value, (list, tuple)):
            return value
        normalized: list[Any] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                normalized.append(item)
                continue
            clean = _normalize_text(item)
            if not clean:
                continue
            key = clean.casefold()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(clean)
        return tuple(normalized)

    @field_validator(
        "interests",
        "expertise",
        "preferred_topics",
        "forbidden_topics",
        "catchphrases",
    )
    @classmethod
    def _validate_list_items(
        cls,
        value: tuple[str, ...],
        info: Any,
    ) -> tuple[str, ...]:
        max_length = _LIST_ITEM_MAX_LENGTHS[info.field_name]
        if any(not item or len(item) > max_length for item in value):
            raise PydanticCustomError(
                "persona_list_item_length",
                f"{info.field_name} item length is invalid",
            )
        return value

    @model_validator(mode="after")
    def _validate_semantics(self) -> PersonaV1:
        preferred = {item.casefold() for item in self.preferred_topics}
        forbidden = {item.casefold() for item in self.forbidden_topics}
        if preferred & forbidden:
            raise PydanticCustomError(
                "persona_topic_overlap",
                "preferred_topics and forbidden_topics must not overlap",
            )

        for catchphrase in self.catchphrases:
            if (
                _URL_RE.search(catchphrase)
                or _PRICE_RE.search(catchphrase)
                or _CONTACT_RE.search(catchphrase)
                or _CTA_RE.search(catchphrase)
            ):
                raise PydanticCustomError(
                    "persona_catchphrase_commercial_content",
                    "catchphrases must not contain URL, price, contact, or CTA content",
                )

        guarded_values = (
            self.tone,
            self.system_prompt,
            *self.interests,
            *self.expertise,
            *self.preferred_topics,
            *self.forbidden_topics,
            *self.catchphrases,
        )
        if any(_contains_prompt_injection(value) for value in guarded_values):
            raise PydanticCustomError(
                "persona_prompt_injection_rejected",
                "Persona contains a prohibited model-control instruction",
            )

        canonical = _canonical_json_from_payload(self.model_dump(mode="json"))
        if len(canonical.encode("utf-8")) > PERSONA_MAX_TOTAL_BYTES:
            raise PydanticCustomError(
                "persona_total_bytes",
                f"normalized Persona exceeds {PERSONA_MAX_TOTAL_BYTES} UTF-8 bytes",
            )
        return self


def normalize_persona_payload(payload: PersonaV1 | Mapping[str, Any]) -> PersonaV1:
    """Return the single normalized v1 domain representation."""

    if isinstance(payload, PersonaV1):
        return payload
    return PersonaV1.model_validate(payload)


def canonical_persona_json(persona: PersonaV1 | Mapping[str, Any]) -> str:
    normalized = normalize_persona_payload(persona)
    return _canonical_json_from_payload(normalized.model_dump(mode="json"))


def hash_persona(persona: PersonaV1 | Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_persona_json(persona).encode("utf-8")).hexdigest()


NEUTRAL_PERSONA = PersonaV1(
    schema_version=PERSONA_SCHEMA_VERSION,
    name="中性群友",
    tone="自然、克制、简短",
    interests=(),
    expertise=(),
    reply_length="short",
    preferred_topics=(),
    forbidden_topics=(),
    ad_style="neutral",
    catchphrases=(),
    language_style="auto",
    system_prompt="",
)
NEUTRAL_PERSONA_HASH = hash_persona(NEUTRAL_PERSONA)


@dataclass(frozen=True, slots=True)
class PersonaSnapshotResult:
    account_id: int
    source: PersonaSource
    revision: int
    persona: PersonaV1
    persona_hash: str


# Name retained from the requirements' domain sketch.  Both imports identify
# the same immutable value type.
EffectivePersona = PersonaSnapshotResult


@dataclass(frozen=True, slots=True)
class AccountPersonaState:
    account_id: int
    configured: bool
    revision: int
    persona: PersonaV1 | None
    persona_hash: str | None
    updated_at: datetime | None
    updated_by: int | None
    changed: bool = False


class AccountPersonaError(Exception):
    """Stable domain failure mapped to the HTTP envelope by the API layer."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 409,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.http_status = status_code
        self.details = dict(details or {})


class _PersonaAccount(Protocol):
    id: int
    account_type: Any
    operation_config: Any
    ai_persona: Mapping[str, Any] | None
    ai_persona_revision: int
    ai_persona_hash: str | None
    ai_persona_updated_at: datetime | None
    ai_persona_updated_by: int | None


class _FlushableSession(Protocol):
    async def flush(self) -> None: ...


def _wire_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _valid_non_negative_revision(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise AccountPersonaError(
            "PERSONA_CONFIG_INVALID",
            "Stored Persona metadata is invalid",
            details={"field": "ai_persona_revision"},
        )
    return value


def _validate_expected_revision(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise AccountPersonaError(
            "PERSONA_VALIDATION_FAILED",
            "Persona validation failed",
            status_code=422,
            details={"field": "expected_revision", "type": "non_negative_integer"},
        )
    return value


def _conflict(state: AccountPersonaState) -> AccountPersonaError:
    return AccountPersonaError(
        "PERSONA_REVISION_CONFLICT",
        "Persona revision conflict",
        details={
            "current_revision": state.revision,
            "configured": state.configured,
            "persona_hash": state.persona_hash,
        },
    )


def _validate_account_applicability(account: _PersonaAccount) -> None:
    if _wire_value(account.account_type) != "promoter":
        raise AccountPersonaError(
            "PERSONA_ACCOUNT_TYPE_UNSUPPORTED",
            "Account type does not support Persona",
        )
    operation_config = getattr(account, "operation_config", None)
    if operation_config is None:
        raise AccountPersonaError(
            "PERSONA_OPERATION_CONFIG_MISSING",
            "Account operation configuration is missing",
        )
    if _wire_value(getattr(operation_config, "operation_mode", None)) != "growth":
        raise AccountPersonaError(
            "PERSONA_ACCOUNT_MODE_UNSUPPORTED",
            "Account operation mode does not support Persona",
        )


class AccountPersonaService:
    """Pure account Persona state transitions.

    Callers must load the five deferred columns and, for writes, lock
    TelegramAccount before AccountOperationConfig.  Methods flush but never
    commit and deliberately do not insert audit rows, allowing the API or
    execution service to add a safe audit summary in the same transaction.
    """

    @staticmethod
    def get(
        account: _PersonaAccount,
        *,
        include_neutral: bool = False,
    ) -> AccountPersonaState:
        revision = _valid_non_negative_revision(account.ai_persona_revision)
        raw_persona = account.ai_persona
        stored_hash = account.ai_persona_hash

        if raw_persona is None:
            if stored_hash is not None:
                raise AccountPersonaError(
                    "PERSONA_CONFIG_INVALID",
                    "Stored Persona metadata is invalid",
                    details={"field": "ai_persona_hash"},
                )
            return AccountPersonaState(
                account_id=int(account.id),
                configured=False,
                revision=revision,
                persona=NEUTRAL_PERSONA if include_neutral else None,
                persona_hash=NEUTRAL_PERSONA_HASH if include_neutral else None,
                updated_at=account.ai_persona_updated_at,
                updated_by=account.ai_persona_updated_by,
            )

        try:
            persona = normalize_persona_payload(raw_persona)
        except Exception as exc:
            raise AccountPersonaError(
                "PERSONA_CONFIG_INVALID",
                "Stored Persona configuration is invalid",
                details={"field": "ai_persona"},
            ) from exc
        computed_hash = hash_persona(persona)
        if revision < 1 or stored_hash != computed_hash:
            raise AccountPersonaError(
                "PERSONA_CONFIG_INVALID",
                "Stored Persona metadata is invalid",
                details={"field": "ai_persona_revision_or_hash"},
            )
        return AccountPersonaState(
            account_id=int(account.id),
            configured=True,
            revision=revision,
            persona=persona,
            persona_hash=computed_hash,
            updated_at=account.ai_persona_updated_at,
            updated_by=account.ai_persona_updated_by,
        )

    @staticmethod
    async def put(
        db: _FlushableSession,
        account: _PersonaAccount,
        payload: PersonaV1 | Mapping[str, Any],
        *,
        expected_revision: int,
        actor_id: int | None,
    ) -> AccountPersonaState:
        _validate_account_applicability(account)
        expected_revision = _validate_expected_revision(expected_revision)
        target = normalize_persona_payload(payload)
        target_hash = hash_persona(target)
        current = AccountPersonaService.get(account)

        # Safe replay is evaluated before revision conflict, including a retry
        # carrying the revision that preceded its already-committed first call.
        if current.configured and current.persona_hash == target_hash:
            return current
        if current.revision != expected_revision:
            raise _conflict(current)

        account.ai_persona = dict(target.model_dump(mode="json"))
        account.ai_persona_revision = current.revision + 1
        account.ai_persona_hash = target_hash
        account.ai_persona_updated_at = datetime.utcnow()
        account.ai_persona_updated_by = actor_id
        await db.flush()
        return replace(AccountPersonaService.get(account), changed=True)

    @staticmethod
    async def reset(
        db: _FlushableSession,
        account: _PersonaAccount,
        *,
        expected_revision: int,
        actor_id: int | None,
    ) -> AccountPersonaState:
        expected_revision = _validate_expected_revision(expected_revision)
        revision = _valid_non_negative_revision(account.ai_persona_revision)

        # A clean SQL NULL state is an idempotent replay even if the client
        # carries the revision from immediately before the first reset.
        if account.ai_persona is None and account.ai_persona_hash is None:
            return AccountPersonaService.get(account)

        if revision != expected_revision:
            configured = account.ai_persona is not None
            raise _conflict(
                AccountPersonaState(
                    account_id=int(account.id),
                    configured=configured,
                    revision=revision,
                    persona=None,
                    persona_hash=account.ai_persona_hash,
                    updated_at=account.ai_persona_updated_at,
                    updated_by=account.ai_persona_updated_by,
                )
            )

        account.ai_persona = None
        account.ai_persona_revision = revision + 1
        account.ai_persona_hash = None
        account.ai_persona_updated_at = datetime.utcnow()
        account.ai_persona_updated_by = actor_id
        await db.flush()
        return replace(AccountPersonaService.get(account), changed=True)

    @staticmethod
    def snapshot_for_new_execution(
        account: _PersonaAccount,
        *,
        feature_enabled: bool,
    ) -> PersonaSnapshotResult:
        _validate_account_applicability(account)
        if not feature_enabled:
            return PersonaSnapshotResult(
                account_id=int(account.id),
                source=PersonaSource.FEATURE_DISABLED_DEFAULT,
                revision=0,
                persona=NEUTRAL_PERSONA,
                persona_hash=NEUTRAL_PERSONA_HASH,
            )

        state = AccountPersonaService.get(account)
        if not state.configured:
            return PersonaSnapshotResult(
                account_id=int(account.id),
                source=PersonaSource.NEUTRAL_DEFAULT,
                revision=0,
                persona=NEUTRAL_PERSONA,
                persona_hash=NEUTRAL_PERSONA_HASH,
            )
        assert state.persona is not None and state.persona_hash is not None
        return PersonaSnapshotResult(
            account_id=int(account.id),
            source=PersonaSource.CONFIGURED,
            revision=state.revision,
            persona=state.persona,
            persona_hash=state.persona_hash,
        )

    @staticmethod
    def snapshot_draft(
        *,
        account_id: int,
        payload: PersonaV1 | Mapping[str, Any],
    ) -> PersonaSnapshotResult:
        persona = normalize_persona_payload(payload)
        return PersonaSnapshotResult(
            account_id=int(account_id),
            source=PersonaSource.DRAFT,
            revision=0,
            persona=persona,
            persona_hash=hash_persona(persona),
        )


__all__ = [
    "AccountPersonaError",
    "AccountPersonaService",
    "AccountPersonaState",
    "AdStyle",
    "EffectivePersona",
    "LanguageStyle",
    "NEUTRAL_PERSONA",
    "NEUTRAL_PERSONA_HASH",
    "PERSONA_MAX_TOTAL_BYTES",
    "PERSONA_PROMPT_TEMPLATE_VERSION",
    "PERSONA_SCHEMA_VERSION",
    "PersonaSnapshotResult",
    "PersonaSource",
    "PersonaV1",
    "ReplyLength",
    "canonical_persona_json",
    "hash_persona",
    "normalize_persona_payload",
]
