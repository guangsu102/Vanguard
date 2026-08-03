"""Fast duplicate checks for group-search keywords."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Optional

import structlog
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis as redis_module
from app.core.ai.keyword_generator import normalize_keyword_text
from app.modules.acquisition.models import GroupSearchKeyword

logger = structlog.get_logger()

SIGNATURE_CACHE_KEY = "vanguard:group_search_keywords:normalized:v1"
SIGNATURE_CACHE_READY_KEY = "vanguard:group_search_keywords:normalized:v1:ready"
SIGNATURE_SEPARATOR = "\x1f"


def normalize_group_search_keyword(text: str) -> str:
    """Return the normalized form used for duplicate checks."""
    return normalize_keyword_text(text)


def build_keyword_signature(keyword_type: str, text: str) -> Optional[str]:
    """Build a stable type-aware duplicate signature."""
    normalized = normalize_group_search_keyword(text)
    if not normalized:
        return None
    return f"{keyword_type}{SIGNATURE_SEPARATOR}{normalized}"


def split_keyword_signature(signature: str) -> tuple[str, str]:
    keyword_type, normalized = signature.split(SIGNATURE_SEPARATOR, 1)
    return keyword_type, normalized


def _redis_client():
    return redis_module.redis_client


async def hydrate_keyword_signature_cache(db: AsyncSession) -> bool:
    """Refresh the Redis duplicate cache from the database when Redis is available."""
    client = _redis_client()
    if client is None:
        return False

    try:
        if await client.exists(SIGNATURE_CACHE_READY_KEY):
            return True

        rows = await db.execute(
            select(
                GroupSearchKeyword.keyword_type,
                GroupSearchKeyword.normalized_text,
                GroupSearchKeyword.text,
            )
        )
        pipe = client.pipeline()
        pipe.delete(SIGNATURE_CACHE_KEY)
        count = 0
        for keyword_type, normalized_text, text in rows.all():
            normalized = normalized_text or normalize_group_search_keyword(text or "")
            if not normalized:
                continue
            pipe.sadd(SIGNATURE_CACHE_KEY, f"{keyword_type}{SIGNATURE_SEPARATOR}{normalized}")
            count += 1
        pipe.set(SIGNATURE_CACHE_READY_KEY, "1", ex=3600)
        await pipe.execute()
        logger.info("group_search_keyword_signature_cache_hydrated", count=count)
        return True
    except Exception as exc:  # pragma: no cover - Redis is an optional accelerator here.
        logger.warning("group_search_keyword_signature_cache_hydrate_failed", error=str(exc))
        return False


async def find_existing_keyword_signatures(
    db: AsyncSession,
    candidates: Iterable[tuple[str, str]],
) -> set[str]:
    """Return signatures that already exist, using Redis first and DB as the source of truth."""
    signatures = {
        signature
        for keyword_type, text in candidates
        if (signature := build_keyword_signature(keyword_type, text))
    }
    if not signatures:
        return set()

    found: set[str] = set()
    client = _redis_client()
    if client is not None:
        try:
            await hydrate_keyword_signature_cache(db)
            signature_list = list(signatures)
            redis_hits = await client.smismember(SIGNATURE_CACHE_KEY, signature_list)
            found.update(signature for signature, exists in zip(signature_list, redis_hits) if exists)
        except Exception as exc:  # pragma: no cover - DB fallback keeps correctness.
            logger.warning("group_search_keyword_signature_cache_lookup_failed", error=str(exc))

    remaining = signatures - found
    if not remaining:
        return found

    filters = []
    for signature in remaining:
        keyword_type, normalized = split_keyword_signature(signature)
        filters.append(
            and_(
                GroupSearchKeyword.keyword_type == keyword_type,
                GroupSearchKeyword.normalized_text == normalized,
            )
        )
    rows = await db.execute(
        select(GroupSearchKeyword.keyword_type, GroupSearchKeyword.normalized_text).where(or_(*filters))
    )
    db_hits = {
        f"{keyword_type}{SIGNATURE_SEPARATOR}{normalized}"
        for keyword_type, normalized in rows.all()
        if normalized
    }
    found.update(db_hits)

    if client is not None and db_hits:
        try:
            await client.sadd(SIGNATURE_CACHE_KEY, *db_hits)
        except Exception as exc:  # pragma: no cover
            logger.debug("group_search_keyword_signature_cache_add_failed", error=str(exc))
    return found


async def add_keyword_signatures(candidates: Iterable[tuple[str, str]]) -> None:
    """Add newly persisted keyword signatures to the Redis accelerator."""
    client = _redis_client()
    if client is None:
        return
    signatures = [
        signature
        for keyword_type, text in candidates
        if (signature := build_keyword_signature(keyword_type, text))
    ]
    if not signatures:
        return
    try:
        await client.sadd(SIGNATURE_CACHE_KEY, *signatures)
    except Exception as exc:  # pragma: no cover
        logger.debug("group_search_keyword_signature_cache_add_failed", error=str(exc))


async def recent_keyword_texts(db: AsyncSession, *, limit: int = 200) -> list[str]:
    """Small prompt hint list; avoids feeding the LLM the whole keyword table."""
    rows = await db.execute(
        select(GroupSearchKeyword.text)
        .where(GroupSearchKeyword.text.is_not(None))
        .order_by(GroupSearchKeyword.updated_at.desc(), GroupSearchKeyword.id.desc())
        .limit(limit)
    )
    return [text for text in rows.scalars().all() if text]
