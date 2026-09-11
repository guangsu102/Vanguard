"""Safe stage-three Persona structured events and logical metric contracts.

Only identifiers, bounded enums, hashes prefixes, integer usage, and timings are
accepted here.  Callers cannot pass Persona values, prompts, context, message
bodies, credentials, sessions, proxies, or sensitive-term text through this
module.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from functools import wraps
from typing import Any, Protocol

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.models import TelegramAccount

logger = structlog.get_logger().bind(module="owned_group_persona_observability")

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")
_SAFE_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REASON_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_PERSONA_SOURCES = frozenset(
    {
        "configured",
        "neutral_default",
        "feature_disabled_default",
        "legacy_default",
        "draft",
        "unknown",
    }
)
_CONTENT_CATEGORIES = frozenset({"community", "promotion"})
_RESULTS = frozenset({"success", "failed", "allowed", "blocked", "skipped"})
_STAGES = frozenset({"generated_body", "generated_final", "review", "send"})
_PROVIDERS = frozenset({"openai", "anthropic", "local", "unknown"})
_USAGE_SOURCES = frozenset({"provider", "estimated", "cache"})

STRUCTURED_EVENT_NAMES = frozenset(
    {
        "account_persona.update",
        "account_persona.resolve",
        "account_persona.snapshot",
        "account_persona.preview",
        "owned_group_prompt.build",
        "owned_group_persona.llm_usage",
        "owned_group_outbound_governance.evaluate",
        "owned_group_persona.growth_ad_config_access",
    }
)

LOGICAL_METRIC_NAMES = frozenset(
    {
        "account_persona_configured",
        "account_persona_update_total",
        "account_persona_resolve_total",
        "account_persona_preview_total",
        "account_persona_preview_duration_seconds",
        "owned_group_persona_prompt_build_total",
        "owned_group_persona_llm_tokens_total",
        "owned_group_persona_outbound_block_total",
        "owned_group_persona_account_mismatch_total",
        "owned_group_persona_growth_ad_config_access_total",
    }
)

# These are the only business-event fields emitted by this module.  The metric
# ``value`` field and its contract labels are emitted by private helpers below.
SAFE_STRUCTURED_EVENT_FIELDS = frozenset(
    {
        "request_id",
        "execution_id",
        "account_id",
        "asset_id",
        "core_group_id",
        "content_category",
        "persona_source",
        "revision",
        "hash_prefix",
        "prompt_template_version",
        "provider",
        "model",
        "input_tokens",
        "output_tokens",
        "cost_microunits",
        "cache_hit",
        "usage_source",
        "stage",
        "result",
        "reason_code",
        "duration_ms",
    }
)


class _EventLogger(Protocol):
    def info(self, event: str, **fields: Any) -> Any: ...


def _log(log: _EventLogger | None, event: str, **fields: Any) -> None:
    if event not in STRUCTURED_EVENT_NAMES and event not in LOGICAL_METRIC_NAMES:
        raise ValueError("unsupported Persona observability event")
    (log or logger).info(event, **{key: value for key, value in fields.items() if value is not None})


def _fail_open[**P](function: Callable[P, None]) -> Callable[P, None]:
    """Keep telemetry validation/logger failures outside business control flow."""

    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> None:
        try:
            function(*args, **kwargs)
        except Exception:
            return

    return wrapped


def _positive_id(value: int | None, name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer or null")
    return value


def _non_negative_int(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _bounded_duration(value: int) -> int:
    value = _non_negative_int(value, "duration_ms")
    return min(value, 86_400_000)


def _safe_optional_id(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SAFE_ID_RE.fullmatch(value):
        return None
    return value


def _safe_name(value: str, name: str) -> str:
    if not isinstance(value, str) or not _SAFE_NAME_RE.fullmatch(value):
        return "unknown"
    return value


def _safe_version(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_VERSION_RE.fullmatch(value):
        raise ValueError("prompt_template_version is invalid")
    return value


def _enum(value: str, allowed: frozenset[str], name: str) -> str:
    if value not in allowed:
        raise ValueError(f"{name} is invalid")
    return value


def _reason(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _REASON_CODE_RE.fullmatch(value):
        return "UNKNOWN"
    return value


def _hash_prefix(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        return None
    return value[:12]


def _event_fields(**fields: Any) -> dict[str, Any]:
    unknown = set(fields) - SAFE_STRUCTURED_EVENT_FIELDS
    if unknown:
        raise ValueError(f"unsafe Persona observability fields: {sorted(unknown)}")
    return {key: value for key, value in fields.items() if value is not None}


def _metric(
    name: str,
    *,
    value: int | float,
    log: _EventLogger | None,
    **labels: str,
) -> None:
    if name not in LOGICAL_METRIC_NAMES:
        raise ValueError("unsupported Persona logical metric")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError("logical metric value must be non-negative")
    _log(log, name, value=value, **labels)


@_fail_open
def record_persona_configured(
    *,
    configured_total: int,
    log: _EventLogger | None = None,
) -> None:
    """Emit the system-level configured Persona gauge without account labels."""

    _metric(
        "account_persona_configured",
        value=_non_negative_int(configured_total, "configured_total"),
        log=log,
    )


async def refresh_persona_configured_gauge(
    db: AsyncSession,
) -> int | None:
    """Refresh the exact configured Persona gauge without affecting callers."""

    try:
        configured_total = await db.scalar(
            select(func.count(TelegramAccount.id)).where(
                TelegramAccount.ai_persona.is_not(None)
            )
        )
        total = int(configured_total or 0)
        record_persona_configured(configured_total=total)
    except Exception:
        return None
    return total


async def refresh_persona_configured_gauge_at_startup() -> int | None:
    """Open a short startup session and fail open on any database failure."""

    try:
        from app.core.database import get_db_session

        async with get_db_session() as db:
            return await refresh_persona_configured_gauge(db)
    except Exception:
        return None


@_fail_open
def record_persona_update(
    *,
    account_id: int,
    revision: int,
    persona_hash: str | None,
    result: str,
    reason_code: str | None = None,
    request_id: str | None = None,
    log: _EventLogger | None = None,
) -> None:
    result = _enum(result, _RESULTS, "result")
    safe_reason = _reason(reason_code)
    _log(
        log,
        "account_persona.update",
        **_event_fields(
            request_id=_safe_optional_id(request_id),
            account_id=_positive_id(account_id, "account_id"),
            revision=_non_negative_int(revision, "revision"),
            hash_prefix=_hash_prefix(persona_hash),
            result=result,
            reason_code=safe_reason,
        ),
    )
    _metric(
        "account_persona_update_total",
        value=1,
        log=log,
        result=result,
        reason_code=safe_reason or "NONE",
    )


@_fail_open
def record_persona_resolve(
    *,
    account_id: int,
    persona_source: str,
    revision: int,
    persona_hash: str | None,
    result: str,
    reason_code: str | None = None,
    request_id: str | None = None,
    log: _EventLogger | None = None,
) -> None:
    source = _enum(persona_source, _PERSONA_SOURCES, "persona_source")
    result = _enum(result, _RESULTS, "result")
    safe_reason = _reason(reason_code)
    _log(
        log,
        "account_persona.resolve",
        **_event_fields(
            request_id=_safe_optional_id(request_id),
            account_id=_positive_id(account_id, "account_id"),
            persona_source=source,
            revision=_non_negative_int(revision, "revision"),
            hash_prefix=_hash_prefix(persona_hash),
            result=result,
            reason_code=safe_reason,
        ),
    )
    _metric(
        "account_persona_resolve_total",
        value=1,
        log=log,
        source=source,
        result=result,
    )


@_fail_open
def record_persona_snapshot(
    *,
    account_id: int,
    persona_source: str,
    revision: int,
    persona_hash: str,
    asset_id: int | None = None,
    core_group_id: int | None = None,
    request_id: str | None = None,
    result: str = "success",
    reason_code: str | None = None,
    log: _EventLogger | None = None,
) -> None:
    _log(
        log,
        "account_persona.snapshot",
        **_event_fields(
            request_id=_safe_optional_id(request_id),
            account_id=_positive_id(account_id, "account_id"),
            asset_id=_positive_id(asset_id, "asset_id"),
            core_group_id=_positive_id(core_group_id, "core_group_id"),
            persona_source=_enum(persona_source, _PERSONA_SOURCES, "persona_source"),
            revision=_non_negative_int(revision, "revision"),
            hash_prefix=_hash_prefix(persona_hash),
            result=_enum(result, _RESULTS, "result"),
            reason_code=_reason(reason_code),
        ),
    )


@_fail_open
def record_persona_preview(
    *,
    account_id: int,
    asset_id: int,
    core_group_id: int | None,
    content_category: str,
    persona_source: str,
    revision: int,
    persona_hash: str | None,
    prompt_template_version: str,
    result: str,
    duration_ms: int,
    reason_code: str | None = None,
    request_id: str | None = None,
    log: _EventLogger | None = None,
) -> None:
    category = _enum(content_category, _CONTENT_CATEGORIES, "content_category")
    result = _enum(result, _RESULTS, "result")
    safe_reason = _reason(reason_code)
    duration_ms = _bounded_duration(duration_ms)
    _log(
        log,
        "account_persona.preview",
        **_event_fields(
            request_id=_safe_optional_id(request_id),
            account_id=_positive_id(account_id, "account_id"),
            asset_id=_positive_id(asset_id, "asset_id"),
            core_group_id=_positive_id(core_group_id, "core_group_id"),
            content_category=category,
            persona_source=_enum(persona_source, _PERSONA_SOURCES, "persona_source"),
            revision=_non_negative_int(revision, "revision"),
            hash_prefix=_hash_prefix(persona_hash),
            prompt_template_version=_safe_version(prompt_template_version),
            result=result,
            reason_code=safe_reason,
            duration_ms=duration_ms,
        ),
    )
    _metric(
        "account_persona_preview_total",
        value=1,
        log=log,
        result=result,
        reason_code=safe_reason or "NONE",
        content_category=category,
    )
    _metric(
        "account_persona_preview_duration_seconds",
        value=duration_ms / 1000,
        log=log,
    )


@_fail_open
def record_prompt_build(
    *,
    execution_id: int | None,
    account_id: int,
    asset_id: int,
    core_group_id: int,
    content_category: str,
    persona_source: str,
    revision: int,
    persona_hash: str,
    prompt_hash: str | None,
    prompt_template_version: str,
    result: str,
    duration_ms: int,
    reason_code: str | None = None,
    request_id: str | None = None,
    log: _EventLogger | None = None,
) -> None:
    category = _enum(content_category, _CONTENT_CATEGORIES, "content_category")
    result = _enum(result, _RESULTS, "result")
    _log(
        log,
        "owned_group_prompt.build",
        **_event_fields(
            request_id=_safe_optional_id(request_id),
            execution_id=_positive_id(execution_id, "execution_id"),
            account_id=_positive_id(account_id, "account_id"),
            asset_id=_positive_id(asset_id, "asset_id"),
            core_group_id=_positive_id(core_group_id, "core_group_id"),
            content_category=category,
            persona_source=_enum(persona_source, _PERSONA_SOURCES, "persona_source"),
            revision=_non_negative_int(revision, "revision"),
            hash_prefix=_hash_prefix(prompt_hash or persona_hash),
            prompt_template_version=_safe_version(prompt_template_version),
            result=result,
            reason_code=_reason(reason_code),
            duration_ms=_bounded_duration(duration_ms),
        ),
    )
    _metric(
        "owned_group_persona_prompt_build_total",
        value=1,
        log=log,
        result=result,
        content_category=category,
    )


def observe_prompt_build_call[T](
    build: Callable[[], T],
    *,
    execution_id: int | None,
    account_id: int,
    asset_id: int,
    core_group_id: int,
    content_category: str,
    persona_source: str,
    revision: int,
    persona_hash: str,
    prompt_template_version: str,
    request_id: str | None = None,
    log: _EventLogger | None = None,
) -> T:
    """Observe a synchronous Prompt build without receiving Prompt inputs."""

    started = time.perf_counter()
    try:
        built = build()
    except Exception as exc:
        record_prompt_build(
            execution_id=execution_id,
            account_id=account_id,
            asset_id=asset_id,
            core_group_id=core_group_id,
            content_category=content_category,
            persona_source=persona_source,
            revision=revision,
            persona_hash=persona_hash,
            prompt_hash=None,
            prompt_template_version=prompt_template_version,
            result="failed",
            reason_code=getattr(exc, "code", "PROMPT_BUILD_FAILED"),
            duration_ms=int((time.perf_counter() - started) * 1000),
            request_id=request_id,
            log=log,
        )
        raise
    record_prompt_build(
        execution_id=execution_id,
        account_id=account_id,
        asset_id=asset_id,
        core_group_id=core_group_id,
        content_category=content_category,
        persona_source=persona_source,
        revision=revision,
        persona_hash=persona_hash,
        prompt_hash=getattr(built, "prompt_hash", None),
        prompt_template_version=prompt_template_version,
        result="success",
        duration_ms=int((time.perf_counter() - started) * 1000),
        request_id=request_id,
        log=log,
    )
    return built


@_fail_open
def record_llm_usage(
    *,
    execution_id: int | None,
    account_id: int,
    asset_id: int,
    content_category: str,
    persona_source: str,
    revision: int,
    persona_hash: str,
    prompt_template_version: str,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_microunits: int,
    cache_hit: bool,
    usage_source: str,
    result: str,
    duration_ms: int,
    request_id: str | None = None,
    reason_code: str | None = None,
    log: _EventLogger | None = None,
) -> None:
    source = _enum(persona_source, _PERSONA_SOURCES, "persona_source")
    provider = provider if provider in _PROVIDERS else "unknown"
    model = _safe_name(model, "model")
    category = _enum(content_category, _CONTENT_CATEGORIES, "content_category")
    result = _enum(result, _RESULTS, "result")
    input_tokens = _non_negative_int(input_tokens, "input_tokens")
    output_tokens = _non_negative_int(output_tokens, "output_tokens")
    _log(
        log,
        "owned_group_persona.llm_usage",
        **_event_fields(
            request_id=_safe_optional_id(request_id),
            execution_id=_positive_id(execution_id, "execution_id"),
            account_id=_positive_id(account_id, "account_id"),
            asset_id=_positive_id(asset_id, "asset_id"),
            content_category=category,
            persona_source=source,
            revision=_non_negative_int(revision, "revision"),
            hash_prefix=_hash_prefix(persona_hash),
            prompt_template_version=_safe_version(prompt_template_version),
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_microunits=_non_negative_int(cost_microunits, "cost_microunits"),
            cache_hit=bool(cache_hit),
            usage_source=_enum(usage_source, _USAGE_SOURCES, "usage_source"),
            result=result,
            reason_code=_reason(reason_code),
            duration_ms=_bounded_duration(duration_ms),
        ),
    )
    for direction, value in (("input", input_tokens), ("output", output_tokens)):
        _metric(
            "owned_group_persona_llm_tokens_total",
            value=value,
            log=log,
            direction=direction,
            provider=provider,
            model=model,
            persona_source=source,
        )


@_fail_open
def record_outbound_evaluation(
    *,
    execution_id: int,
    account_id: int,
    asset_id: int,
    core_group_id: int,
    content_category: str,
    persona_source: str,
    stage: str,
    allowed: bool,
    reason_code: str | None,
    governance_hash: str | None,
    duration_ms: int,
    request_id: str | None = None,
    log: _EventLogger | None = None,
) -> None:
    category = _enum(content_category, _CONTENT_CATEGORIES, "content_category")
    stage = _enum(stage, _STAGES, "stage")
    safe_reason = _reason(reason_code)
    _log(
        log,
        "owned_group_outbound_governance.evaluate",
        **_event_fields(
            request_id=_safe_optional_id(request_id),
            execution_id=_positive_id(execution_id, "execution_id"),
            account_id=_positive_id(account_id, "account_id"),
            asset_id=_positive_id(asset_id, "asset_id"),
            core_group_id=_positive_id(core_group_id, "core_group_id"),
            content_category=category,
            persona_source=_enum(persona_source, _PERSONA_SOURCES, "persona_source"),
            hash_prefix=_hash_prefix(governance_hash),
            stage=stage,
            result="allowed" if allowed else "blocked",
            reason_code=safe_reason,
            duration_ms=_bounded_duration(duration_ms),
        ),
    )
    if not allowed:
        _metric(
            "owned_group_persona_outbound_block_total",
            value=1,
            log=log,
            stage=stage,
            reason_code=safe_reason or "CONTENT_POLICY_BLOCKED",
            content_category=category,
        )


@_fail_open
def record_persona_account_mismatch(
    *,
    account_id: int,
    persona_source: str,
    revision: int,
    persona_hash: str | None,
    request_id: str | None = None,
    log: _EventLogger | None = None,
) -> None:
    record_persona_resolve(
        account_id=account_id,
        persona_source=persona_source,
        revision=revision,
        persona_hash=persona_hash,
        result="failed",
        reason_code="PERSONA_ACCOUNT_MISMATCH",
        request_id=request_id,
        log=log,
    )
    _metric("owned_group_persona_account_mismatch_total", value=1, log=log)


@_fail_open
def record_growth_ad_config_access(
    *,
    reason_code: str = "PERSONA_GROWTH_AD_CONFIG_ACCESS",
    log: _EventLogger | None = None,
) -> None:
    """Tripwire to call only if a Persona path reaches growth-ad configuration."""

    _log(
        log,
        "owned_group_persona.growth_ad_config_access",
        **_event_fields(result="blocked", reason_code=_reason(reason_code)),
    )
    _metric("owned_group_persona_growth_ad_config_access_total", value=1, log=log)


__all__ = [
    "LOGICAL_METRIC_NAMES",
    "SAFE_STRUCTURED_EVENT_FIELDS",
    "STRUCTURED_EVENT_NAMES",
    "observe_prompt_build_call",
    "refresh_persona_configured_gauge",
    "refresh_persona_configured_gauge_at_startup",
    "record_growth_ad_config_access",
    "record_llm_usage",
    "record_outbound_evaluation",
    "record_persona_account_mismatch",
    "record_persona_configured",
    "record_persona_preview",
    "record_persona_resolve",
    "record_persona_snapshot",
    "record_persona_update",
    "record_prompt_build",
]
