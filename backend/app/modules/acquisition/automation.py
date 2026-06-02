"""
Acquisition automation services.

This module wires the existing keyword, group, account-pool, and message
components into background-friendly workflows:
- AI keyword replenishment
- automatic public-group discovery and joining
- advertisement delivery after join or on a schedule
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.account.models import AccountOperationConfig, AccountStatus, AccountType, TelegramAccount
from app.core.account.pool import AccountPool, get_account_pool
from app.core.ai.keyword_generator import KeywordGenerator
from app.core.ai.llm_client import LLMClient, LLMProvider
from app.core.config import settings
from app.core.group.manager import GroupManager
from app.core.group.models import Group, GroupAccountMembership
from app.core.keyword.models import KeywordType
from app.modules.acquisition.config import AcquisitionConfig
from app.modules.acquisition.models import (
    AccountAdBinding,
    AdCampaign,
    AdCreative,
    AdCreativeType,
    AdDeliveryLog,
    AdSendMode,
    AutoJoinAttempt,
    DeliveryStatus,
    GroupSearchKeyword,
    SearchKeywordSource,
    SearchKeywordStatus,
)
from app.modules.acquisition.search.filters import GroupFilter, GroupFilterCriteria
from app.modules.acquisition.search.group_finder import DiscoveredGroup, GroupFinder

logger = structlog.get_logger()

DEFAULT_KEYWORD_TYPE_VALUES = [
    KeywordType.DEMAND.value,
    KeywordType.INQUIRY.value,
    KeywordType.PRICE.value,
    KeywordType.COMPETITOR.value,
]

DEFAULT_KEYWORD_TYPES = [KeywordType(value) for value in DEFAULT_KEYWORD_TYPE_VALUES]

DEFAULT_KEYWORD_MINIMUMS = {
    KeywordType.DEMAND.value: 50,
    KeywordType.INQUIRY.value: 30,
    KeywordType.PRICE.value: 20,
    KeywordType.COMPETITOR.value: 30,
}

DEFAULT_KEYWORD_GENERATE_COUNTS = {
    KeywordType.DEMAND.value: 20,
    KeywordType.INQUIRY.value: 15,
    KeywordType.PRICE.value: 10,
    KeywordType.COMPETITOR.value: 15,
}


def _now() -> datetime:
    return datetime.utcnow()


def _day_start(now: Optional[datetime] = None) -> datetime:
    current = now or _now()
    return current.replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass
class AutomationRunResult:
    """Structured result for automation runs."""

    processed: int = 0
    created: int = 0
    updated: int = 0
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    details: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "processed": self.processed,
            "created": self.created,
            "updated": self.updated,
            "succeeded": self.succeeded,
            "skipped": self.skipped,
            "failed": self.failed,
            "errors": self.errors,
            "details": self.details,
        }


class AcquisitionAutomationService:
    """High-level automation service for acquisition workflows."""

    def __init__(
        self,
        db: AsyncSession,
        account_pool: Optional[AccountPool] = None,
        config: Optional[AcquisitionConfig] = None,
    ):
        self.db = db
        self.account_pool = account_pool or get_account_pool()
        self.config = config or AcquisitionConfig()
        self.group_manager = GroupManager(db)
        self.group_finder = GroupFinder(self.account_pool)
        self.group_filter = GroupFilter(
            GroupFilterCriteria(
                min_members=self.config.search.min_group_members,
                max_members=self.config.search.max_group_members,
                exclude_private=True,
                require_username=True,
            )
        )
        self.logger = logger.bind(module="acquisition_automation")

    # ------------------------------------------------------------------
    # Keyword replenishment
    # ------------------------------------------------------------------

    async def replenish_keywords(
        self,
        *,
        min_per_type: Optional[dict[str, int]] = None,
        generate_counts: Optional[dict[str, int]] = None,
        auto_approve: bool = False,
    ) -> dict[str, Any]:
        """
        Generate missing keywords with AI and add them to the moderation queue.

        Generated keywords are pending by default. Pass auto_approve=True only
        when operators want AI-generated terms to become active immediately.
        """
        min_per_type = min_per_type or DEFAULT_KEYWORD_MINIMUMS.copy()
        generate_counts = generate_counts or DEFAULT_KEYWORD_GENERATE_COUNTS.copy()

        result = AutomationRunResult()
        provider = LLMProvider(settings.LLM_PROVIDER) if settings.LLM_PROVIDER in {p.value for p in LLMProvider} else LLMProvider.OPENAI
        api_key = settings.OPENAI_API_KEY if provider == LLMProvider.OPENAI else settings.ANTHROPIC_API_KEY
        generator = KeywordGenerator(LLMClient(provider=provider, api_key=api_key))

        existing_rows = await self.db.execute(select(GroupSearchKeyword.text))
        existing_texts = {text.lower() for text in existing_rows.scalars().all()}

        for type_value, minimum in min_per_type.items():
            try:
                keyword_type = KeywordType(type_value)
            except ValueError:
                result.skipped += 1
                result.errors.append(f"invalid keyword type: {type_value}")
                continue

            active_count_result = await self.db.execute(
                select(func.count(GroupSearchKeyword.id)).where(
                    GroupSearchKeyword.keyword_type == keyword_type.value,
                    GroupSearchKeyword.status.in_(
                        [SearchKeywordStatus.APPROVED, SearchKeywordStatus.PENDING]
                    ),
                    GroupSearchKeyword.enabled == True,
                )
            )
            current_count = active_count_result.scalar() or 0
            if current_count >= minimum:
                result.skipped += 1
                result.details.append(
                    {"type": type_value, "current": current_count, "minimum": minimum, "action": "skip"}
                )
                continue

            count = max(1, generate_counts.get(type_value, minimum - current_count))
            generated = await generator.generate(category=type_value, count=count)
            status = SearchKeywordStatus.APPROVED if auto_approve else SearchKeywordStatus.PENDING
            added_for_type = 0

            for item in generated:
                text = item.text.strip()
                if not text or text.lower() in existing_texts:
                    continue

                keyword = GroupSearchKeyword(
                    text=text,
                    keyword_type=keyword_type.value,
                    status=status,
                    source=SearchKeywordSource.AUTOMATION,
                    match_mode="fuzzy",
                    requires_review=not auto_approve,
                    enabled=True,
                )
                self.db.add(keyword)
                existing_texts.add(text.lower())
                added_for_type += 1

            await self.db.commit()
            result.processed += 1
            result.created += added_for_type
            result.details.append(
                {
                    "type": type_value,
                    "current": current_count,
                    "minimum": minimum,
                    "generated": len(generated),
                    "created": added_for_type,
                    "status": status.value,
                }
            )

        return result.as_dict()

    # ------------------------------------------------------------------
    # Auto-join
    # ------------------------------------------------------------------

    async def run_auto_join(
        self,
        *,
        max_accounts: int = 10,
        keywords_per_account: int = 5,
        max_groups_per_keyword: int = 10,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Run one automatic group discovery and join pass."""
        result = AutomationRunResult()
        now = _now()

        configs = await self._list_join_enabled_account_configs(max_accounts)
        if not configs:
            return result.as_dict()

        await self._sync_account_pool([config.account for config in configs])

        for op_config in configs:
            result.processed += 1
            account = op_config.account

            if op_config.next_join_after and op_config.next_join_after > now:
                result.skipped += 1
                result.details.append(
                    {
                        "account_id": account.id,
                        "action": "skip",
                        "reason": "join_interval",
                        "next_join_after": op_config.next_join_after.isoformat(),
                    }
                )
                continue

            quota_reason = await self._check_join_quota(op_config)
            if quota_reason:
                result.skipped += 1
                result.details.append({"account_id": account.id, "action": "skip", "reason": quota_reason})
                continue

            keywords = await self._get_search_keywords(op_config, limit=keywords_per_account)
            if len(keywords) < keywords_per_account:
                replenish_result = await self._ensure_keywords_for_auto_join(
                    op_config,
                    current_keywords=keywords,
                    target_count=keywords_per_account,
                )
                if replenish_result:
                    result.updated += replenish_result.get("created", 0)
                    detail = replenish_result.get("detail")
                    if detail:
                        result.details.append(detail)
                    keywords = replenish_result.get("keywords", keywords)
            if not keywords:
                result.skipped += 1
                result.details.append({"account_id": account.id, "action": "skip", "reason": "no_keywords"})
                continue

            joined = False
            for keyword in keywords:
                try:
                    groups = await self.group_finder.search_by_keyword(
                        keyword.text,
                        limit=max_groups_per_keyword,
                        account_id=account.id,
                    )
                    candidates = self.group_filter.filter_groups(groups)
                except Exception as exc:
                    result.failed += 1
                    result.errors.append(f"account {account.id} keyword {keyword.text}: {exc}")
                    continue

                for group, score in candidates:
                    if not group.username:
                        await self._record_join_attempt(
                            account.id,
                            group,
                            DeliveryStatus.SKIPPED,
                            source_keyword=keyword.text,
                            reason="public_username_required",
                        )
                        result.skipped += 1
                        continue

                    if await self._is_already_joined(account.id, group):
                        await self._record_join_attempt(
                            account.id,
                            group,
                            DeliveryStatus.SKIPPED,
                            source_keyword=keyword.text,
                            reason="already_joined",
                        )
                        result.skipped += 1
                        continue

                    db_group = await self._ensure_group(group, keyword.text)

                    if dry_run:
                        await self._record_join_attempt(
                            account.id,
                            group,
                            DeliveryStatus.SKIPPED,
                            db_group=db_group,
                            source_keyword=keyword.text,
                            reason=f"dry_run_score_{score:.1f}",
                        )
                        result.skipped += 1
                        joined = True
                        break

                    try:
                        await self._join_group(account.id, group)
                        await self.group_manager.record_account_membership(
                            db_group.id,
                            account.id,
                            status="joined",
                            join_method="auto_keyword_search",
                            source_keyword=keyword.text,
                        )
                        await self._evaluate_joined_group(account.id, db_group)
                        await self._record_join_attempt(
                            account.id,
                            group,
                            DeliveryStatus.SUCCESS,
                            db_group=db_group,
                            source_keyword=keyword.text,
                            joined_at=_now(),
                        )
                        self._schedule_next_join(op_config)
                        await self.db.commit()
                        result.succeeded += 1
                        result.details.append(
                            {
                                "account_id": account.id,
                                "group_id": db_group.id,
                                "telegram_group_id": group.group_id,
                                "keyword": keyword.text,
                                "score": round(score, 2),
                                "action": "joined",
                            }
                        )
                        joined = True
                        break
                    except Exception as exc:
                        await self._record_join_attempt(
                            account.id,
                            group,
                            DeliveryStatus.FAILED,
                            db_group=db_group,
                            source_keyword=keyword.text,
                            error=str(exc),
                        )
                        result.failed += 1
                        result.errors.append(f"join failed account={account.id} group={group.group_id}: {exc}")

                if joined:
                    break

            if not joined:
                result.skipped += 1

        return result.as_dict()

    async def _list_join_enabled_account_configs(self, limit: int) -> list[AccountOperationConfig]:
        query = (
            select(AccountOperationConfig)
            .options(selectinload(AccountOperationConfig.account))
            .where(
                AccountOperationConfig.enabled == True,
                AccountOperationConfig.auto_join_enabled == True,
            )
            .limit(limit)
        )
        rows = await self.db.execute(query)
        configs = []
        for config in rows.scalars().all():
            account = config.account
            if not account or not account.is_active:
                continue
            if account.status in [AccountStatus.ERROR, AccountStatus.BANNED]:
                continue
            if account.account_type != AccountType.PROMOTER:
                continue
            configs.append(config)
        return configs

    async def _sync_account_pool(self, accounts: list[TelegramAccount]) -> None:
        if not accounts:
            return
        await self.account_pool.sync_from_db(accounts)

    async def _check_join_quota(self, config: AccountOperationConfig) -> Optional[str]:
        now = _now()
        today = _day_start(now)
        joined_today = await self.db.execute(
            select(func.count(AutoJoinAttempt.id)).where(
                AutoJoinAttempt.account_id == config.account_id,
                AutoJoinAttempt.status == DeliveryStatus.SUCCESS.value,
                AutoJoinAttempt.joined_at >= today,
            )
        )
        if (joined_today.scalar() or 0) >= config.max_groups_per_day:
            return "daily_join_quota"

        membership_count = await self.db.execute(
            select(func.count(GroupAccountMembership.id)).where(
                GroupAccountMembership.account_id == config.account_id,
                GroupAccountMembership.status == "joined",
            )
        )
        if (membership_count.scalar() or 0) >= config.max_groups_total:
            return "total_group_quota"

        return None

    async def _get_search_keywords(
        self,
        config: AccountOperationConfig,
        limit: int,
    ) -> list[GroupSearchKeyword]:
        allowed_types = self._parse_keyword_types(config.keyword_types)
        query = select(GroupSearchKeyword).where(
            GroupSearchKeyword.status == SearchKeywordStatus.APPROVED,
            GroupSearchKeyword.enabled == True,
        )
        if allowed_types:
            query = query.where(GroupSearchKeyword.keyword_type.in_([item.value for item in allowed_types]))
        query = query.order_by(GroupSearchKeyword.trigger_count.desc(), GroupSearchKeyword.updated_at.desc()).limit(limit)
        rows = await self.db.execute(query)
        return list(rows.scalars().all())

    def _parse_keyword_types(self, raw: Optional[str]) -> list[KeywordType]:
        if not raw:
            return DEFAULT_KEYWORD_TYPES.copy()
        try:
            values = json.loads(raw)
            return [KeywordType(value) for value in values if value in {item.value for item in KeywordType}]
        except (TypeError, json.JSONDecodeError, ValueError):
            return DEFAULT_KEYWORD_TYPES.copy()

    async def _ensure_keywords_for_auto_join(
        self,
        config: AccountOperationConfig,
        *,
        current_keywords: list[GroupSearchKeyword],
        target_count: int,
    ) -> Optional[dict[str, Any]]:
        if len(current_keywords) >= target_count:
            return None

        allowed_types = self._parse_keyword_types(config.keyword_types)
        current_count = len(current_keywords)
        shortage = max(0, target_count - current_count)
        if shortage <= 0:
            return None

        if not config.keyword_auto_replenish_enabled:
            return {
                "created": 0,
                "keywords": current_keywords,
                "detail": {
                    "account_id": config.account_id,
                    "action": "keyword_replenish_skipped",
                    "reason": "disabled",
                    "current_keywords": current_count,
                    "required_keywords": target_count,
                    "allowed_keyword_types": [item.value for item in allowed_types],
                },
            }

        missing_types = await self._find_missing_keyword_types(allowed_types)
        replenish_types = missing_types or allowed_types
        generate_counts = {
            keyword_type.value: max(
                DEFAULT_KEYWORD_GENERATE_COUNTS.get(keyword_type.value, 10),
                shortage,
            )
            for keyword_type in replenish_types
        }
        min_per_type = {
            keyword_type.value: max(
                await self._count_searchable_keywords(keyword_type) + shortage,
                DEFAULT_KEYWORD_MINIMUMS.get(keyword_type.value, target_count),
            )
            for keyword_type in replenish_types
        }

        replenish_result = await self.replenish_keywords(
            min_per_type=min_per_type,
            generate_counts=generate_counts,
            auto_approve=not config.keyword_replenish_requires_review,
        )
        refreshed_keywords = await self._get_search_keywords(config, limit=target_count)
        detail = {
            "account_id": config.account_id,
            "action": "keyword_replenished",
            "created": replenish_result.get("created", 0),
            "auto_approved": not config.keyword_replenish_requires_review,
            "requires_review": config.keyword_replenish_requires_review,
            "current_keywords": current_count,
            "required_keywords": target_count,
            "available_keywords_after": len(refreshed_keywords),
            "replenish_types": [item.value for item in replenish_types],
        }
        if config.keyword_replenish_requires_review:
            detail["reason"] = "awaiting_review"
        return {
            "created": replenish_result.get("created", 0),
            "keywords": refreshed_keywords,
            "detail": detail,
        }

    async def _find_missing_keyword_types(self, allowed_types: list[KeywordType]) -> list[KeywordType]:
        missing: list[KeywordType] = []
        for keyword_type in allowed_types:
            if await self._count_searchable_keywords(keyword_type) <= 0:
                missing.append(keyword_type)
        return missing

    async def _count_searchable_keywords(self, keyword_type: KeywordType) -> int:
        count_result = await self.db.execute(
            select(func.count(GroupSearchKeyword.id)).where(
                GroupSearchKeyword.keyword_type == keyword_type.value,
                GroupSearchKeyword.status == SearchKeywordStatus.APPROVED,
                GroupSearchKeyword.enabled == True,
            )
        )
        return count_result.scalar() or 0

    async def _is_already_joined(self, account_id: int, group: DiscoveredGroup) -> bool:
        existing_group = await self.group_manager.get_group_by_telegram_id(group.group_id)
        if not existing_group:
            return False
        membership = await self.db.execute(
            select(GroupAccountMembership.id).where(
                GroupAccountMembership.group_id == existing_group.id,
                GroupAccountMembership.account_id == account_id,
                GroupAccountMembership.status == "joined",
            )
        )
        return membership.scalar_one_or_none() is not None

    async def _ensure_group(self, group: DiscoveredGroup, keyword: str) -> Group:
        existing = await self.group_manager.get_group_by_telegram_id(group.group_id)
        if existing:
            return existing
        return await self.group_manager.add_group(
            group_id=group.group_id,
            title=group.title,
            username=group.username,
            member_count=group.member_count,
            source_keyword=keyword,
            discovery_source="auto_keyword_search",
        )

    async def _join_group(self, account_id: int, group: DiscoveredGroup) -> None:
        account = await self.account_pool.acquire_by_id(account_id, purpose="auto_join")
        if account is None:
            raise RuntimeError("account unavailable")
        try:
            client = account.client
            if client is None:
                raise RuntimeError("telegram client unavailable")

            from telethon.tl.functions.channels import JoinChannelRequest

            if not group.username:
                raise RuntimeError("public username is required for auto join")
            target = group.username.lstrip("@")
            entity = await client.get_entity(target)
            await client(JoinChannelRequest(entity))
        finally:
            await self.account_pool.release(account)

    async def _evaluate_joined_group(self, account_id: int, group: Group) -> None:
        try:
            account = await self.account_pool.acquire_by_id(account_id, purpose="group_evaluate")
        except Exception as exc:
            self.logger.warning("group_evaluation_account_unavailable", group_id=group.id, error=str(exc))
            return
        if account is None:
            return
        try:
            client = account.client
            if client is None:
                return

            entity = await client.get_entity(group.username or group.group_id)
            member_count = getattr(entity, "participants_count", None)
            if member_count is not None:
                await self.group_manager.update_group(group.id, member_count=int(member_count))

            messages = []
            if hasattr(client, "iter_messages"):
                async for message in client.iter_messages(entity, limit=20):
                    messages.append(message)

            unique_senders = {getattr(message, "sender_id", None) for message in messages if getattr(message, "sender_id", None)}
            activity_score = min(100, len(messages) * 3 + len(unique_senders) * 5)
            admin_score = 65 if len(unique_senders) >= 5 else 45
            rule_score = 70 if group.username else 50
            history_score = 50

            await self.group_manager.update_scores(
                group.id,
                rule_score=rule_score,
                admin_score=admin_score,
                history_score=history_score,
                activity_score=activity_score,
            )
        except Exception as exc:
            self.logger.warning("group_evaluation_failed", group_id=group.id, error=str(exc))
        finally:
            await self.account_pool.release(account)

    async def _record_join_attempt(
        self,
        account_id: int,
        group: DiscoveredGroup,
        status: DeliveryStatus,
        *,
        db_group: Optional[Group] = None,
        source_keyword: Optional[str] = None,
        reason: Optional[str] = None,
        error: Optional[str] = None,
        joined_at: Optional[datetime] = None,
    ) -> AutoJoinAttempt:
        attempt = AutoJoinAttempt(
            account_id=account_id,
            group_id=db_group.id if db_group else None,
            telegram_group_id=group.group_id,
            group_username=group.username,
            group_title=group.title,
            source_keyword=source_keyword or group.source_keyword,
            status=status.value,
            reason=reason,
            error=error,
            joined_at=joined_at,
        )
        self.db.add(attempt)
        await self.db.commit()
        return attempt

    def _schedule_next_join(self, config: AccountOperationConfig) -> None:
        min_seconds = max(60, config.join_interval_min_seconds)
        max_seconds = max(min_seconds, config.join_interval_max_seconds)
        config.next_join_after = _now() + timedelta(seconds=random.randint(min_seconds, max_seconds))

    # ------------------------------------------------------------------
    # Advertisement delivery
    # ------------------------------------------------------------------

    async def run_ad_delivery(self, *, max_deliveries: int = 20, dry_run: bool = False) -> dict[str, Any]:
        """Run one advertisement delivery pass."""
        result = AutomationRunResult()
        bindings = await self._list_enabled_ad_bindings()
        if not bindings:
            return result.as_dict()

        account_ids = {binding.account_id for binding in bindings}
        accounts = await self.db.execute(select(TelegramAccount).where(TelegramAccount.id.in_(account_ids)))
        await self._sync_account_pool(list(accounts.scalars().all()))

        for binding in bindings:
            if result.succeeded >= max_deliveries:
                break

            campaign = binding.campaign
            if not self._campaign_is_active(campaign):
                result.skipped += 1
                continue

            creative = binding.creative or await self._choose_creative()
            if creative is None:
                result.skipped += 1
                result.details.append({"binding_id": binding.id, "reason": "no_creative"})
                continue

            memberships = await self._list_joined_groups_for_account(binding.account_id)
            for membership in memberships:
                if result.succeeded >= max_deliveries:
                    break

                group = membership.group
                if not group:
                    continue
                result.processed += 1

                skip_reason = await self._ad_skip_reason(binding, campaign, creative, membership)
                if skip_reason:
                    result.skipped += 1
                    continue

                if dry_run:
                    result.skipped += 1
                    result.details.append(
                        {
                            "account_id": binding.account_id,
                            "group_id": group.id,
                            "campaign_id": campaign.id,
                            "creative_id": creative.id,
                            "action": "dry_run",
                        }
                    )
                    continue

                try:
                    message_id = await self._send_ad(binding.account_id, membership.telegram_group_id, creative)
                    await self._record_ad_delivery(
                        binding.account_id,
                        group,
                        campaign,
                        creative,
                        DeliveryStatus.SUCCESS,
                        telegram_message_id=message_id,
                        sent_at=_now(),
                    )
                    result.succeeded += 1
                    result.details.append(
                        {
                            "account_id": binding.account_id,
                            "group_id": group.id,
                            "campaign_id": campaign.id,
                            "creative_id": creative.id,
                            "message_id": message_id,
                        }
                    )
                except Exception as exc:
                    await self._record_ad_delivery(
                        binding.account_id,
                        group,
                        campaign,
                        creative,
                        DeliveryStatus.FAILED,
                        error=str(exc),
                    )
                    result.failed += 1
                    result.errors.append(
                        f"ad delivery failed account={binding.account_id} group={group.group_id}: {exc}"
                    )

        return result.as_dict()

    async def _list_enabled_ad_bindings(self) -> list[AccountAdBinding]:
        rows = await self.db.execute(
            select(AccountAdBinding)
            .options(
                selectinload(AccountAdBinding.account),
                selectinload(AccountAdBinding.campaign),
                selectinload(AccountAdBinding.creative),
            )
            .where(AccountAdBinding.enabled == True)
            .order_by(AccountAdBinding.priority.desc(), AccountAdBinding.id)
        )
        return list(rows.scalars().all())

    def _campaign_is_active(self, campaign: AdCampaign) -> bool:
        now = _now()
        if not campaign.enabled:
            return False
        if campaign.start_at and now < campaign.start_at:
            return False
        if campaign.end_at and now > campaign.end_at:
            return False
        return True

    async def _choose_creative(self) -> Optional[AdCreative]:
        rows = await self.db.execute(
            select(AdCreative).where(AdCreative.enabled == True).order_by(AdCreative.weight.desc(), AdCreative.id)
        )
        return rows.scalars().first()

    async def _list_joined_groups_for_account(self, account_id: int) -> list[GroupAccountMembership]:
        rows = await self.db.execute(
            select(GroupAccountMembership)
            .options(selectinload(GroupAccountMembership.group))
            .where(
                GroupAccountMembership.account_id == account_id,
                GroupAccountMembership.status == "joined",
            )
            .order_by(GroupAccountMembership.joined_at.desc())
        )
        return list(rows.scalars().all())

    async def _ad_skip_reason(
        self,
        binding: AccountAdBinding,
        campaign: AdCampaign,
        creative: AdCreative,
        membership: GroupAccountMembership,
    ) -> Optional[str]:
        del creative
        group = membership.group
        if group is None:
            return "group_missing"

        levels = campaign.get_target_levels()
        if group.level.value not in levels:
            return "group_level_not_targeted"

        now = _now()
        op_config = await self._get_account_operation_config(binding.account_id)
        if op_config and (not op_config.enabled or not op_config.auto_ads_enabled):
            return "account_ads_disabled"
        if op_config and self._in_quiet_hours(op_config, now):
            return "account_quiet_hours"

        group_operation = await self.group_manager.get_operation_config(group)
        if not group_operation.get("can_send_ads", False):
            return "group_level_disallows_ads"

        today = _day_start(now)
        account_daily_limit = op_config.max_messages_per_day if op_config else campaign.max_sends_per_account_per_day
        account_sent_today = await self.db.execute(
            select(func.count(AdDeliveryLog.id)).where(
                AdDeliveryLog.account_id == binding.account_id,
                AdDeliveryLog.status == DeliveryStatus.SUCCESS.value,
                AdDeliveryLog.sent_at >= today,
            )
        )
        if (account_sent_today.scalar() or 0) >= min(account_daily_limit, campaign.max_sends_per_account_per_day):
            return "account_daily_message_quota"

        group_sent_today = await self.db.execute(
            select(func.count(AdDeliveryLog.id)).where(
                AdDeliveryLog.telegram_group_id == membership.telegram_group_id,
                AdDeliveryLog.ad_campaign_id == campaign.id,
                AdDeliveryLog.status == DeliveryStatus.SUCCESS.value,
                AdDeliveryLog.sent_at >= today,
            )
        )
        if (group_sent_today.scalar() or 0) >= campaign.max_sends_per_group_per_day:
            return "group_campaign_daily_quota"

        if campaign.send_mode == AdSendMode.AFTER_JOIN.value:
            if membership.joined_at and now < membership.joined_at + timedelta(minutes=campaign.min_wait_after_join_minutes):
                return "waiting_after_join"
            sent_before = await self.db.execute(
                select(func.count(AdDeliveryLog.id)).where(
                    AdDeliveryLog.telegram_group_id == membership.telegram_group_id,
                    AdDeliveryLog.ad_campaign_id == campaign.id,
                    AdDeliveryLog.status == DeliveryStatus.SUCCESS.value,
                )
            )
            if (sent_before.scalar() or 0) > 0:
                return "after_join_already_sent"

        if campaign.send_mode == AdSendMode.INTERVAL.value:
            last_sent = await self.db.execute(
                select(func.max(AdDeliveryLog.sent_at)).where(
                    AdDeliveryLog.telegram_group_id == membership.telegram_group_id,
                    AdDeliveryLog.ad_campaign_id == campaign.id,
                    AdDeliveryLog.status == DeliveryStatus.SUCCESS.value,
                )
            )
            sent_at = last_sent.scalar()
            if sent_at and now < sent_at + timedelta(minutes=campaign.interval_minutes):
                return "interval_not_due"

        if campaign.send_mode == AdSendMode.SCHEDULED.value:
            if not self._is_scheduled_time_due(campaign, now):
                return "scheduled_time_not_due"

        if op_config:
            last_account_sent = await self.db.execute(
                select(func.max(AdDeliveryLog.sent_at)).where(
                    AdDeliveryLog.account_id == binding.account_id,
                    AdDeliveryLog.status == DeliveryStatus.SUCCESS.value,
                )
            )
            sent_at = last_account_sent.scalar()
            if sent_at and now < sent_at + timedelta(seconds=op_config.message_interval_seconds):
                return "account_message_interval"

        return None

    def _in_quiet_hours(self, config: AccountOperationConfig, now: datetime) -> bool:
        """Return True when account delivery is inside configured quiet hours."""
        if not config.quiet_hours_start or not config.quiet_hours_end:
            return False

        try:
            start_hour, start_minute = [int(part) for part in config.quiet_hours_start.split(":", 1)]
            end_hour, end_minute = [int(part) for part in config.quiet_hours_end.split(":", 1)]
        except (ValueError, AttributeError):
            return False

        current = now.hour * 60 + now.minute
        start = start_hour * 60 + start_minute
        end = end_hour * 60 + end_minute
        if start == end:
            return False
        if start < end:
            return start <= current < end
        return current >= start or current < end

    async def _get_account_operation_config(self, account_id: int) -> Optional[AccountOperationConfig]:
        row = await self.db.execute(
            select(AccountOperationConfig).where(AccountOperationConfig.account_id == account_id)
        )
        return row.scalar_one_or_none()

    def _is_scheduled_time_due(self, campaign: AdCampaign, now: datetime) -> bool:
        current_minutes = now.hour * 60 + now.minute
        for item in campaign.get_scheduled_times():
            try:
                hour, minute = item.split(":", 1)
                scheduled_minutes = int(hour) * 60 + int(minute)
            except (ValueError, AttributeError):
                continue
            if abs(current_minutes - scheduled_minutes) <= 5:
                return True
        return False

    async def _send_ad(self, account_id: int, telegram_group_id: int, creative: AdCreative) -> Optional[int]:
        account = await self.account_pool.acquire_by_id(account_id, purpose="ad_delivery")
        if account is None:
            raise RuntimeError("account unavailable")
        try:
            client = account.client
            if client is None:
                raise RuntimeError("telegram client unavailable")

            content = self._render_ad_content(creative)
            if creative.creative_type in [AdCreativeType.IMAGE.value, AdCreativeType.MIXED.value] and creative.media_url:
                result = await client.send_file(telegram_group_id, creative.media_url, caption=content)
            else:
                result = await client.send_message(telegram_group_id, content)
            account.record_message(success=True)
            return getattr(result, "id", getattr(result, "message_id", None))
        except Exception:
            account.record_message(success=False)
            raise
        finally:
            await self.account_pool.release(account)

    def _render_ad_content(self, creative: AdCreative) -> str:
        content = creative.content
        if creative.link_url:
            content = content.replace("{{link_url}}", creative.link_url)
            if "{{link_url}}" not in creative.content and creative.link_url not in content:
                content = f"{content}\n{creative.link_url}"
        return content

    async def _record_ad_delivery(
        self,
        account_id: int,
        group: Group,
        campaign: AdCampaign,
        creative: AdCreative,
        status: DeliveryStatus,
        *,
        telegram_message_id: Optional[int] = None,
        error: Optional[str] = None,
        sent_at: Optional[datetime] = None,
    ) -> AdDeliveryLog:
        log = AdDeliveryLog(
            account_id=account_id,
            group_id=group.id,
            telegram_group_id=group.group_id,
            ad_campaign_id=campaign.id,
            creative_id=creative.id,
            status=status.value,
            telegram_message_id=telegram_message_id,
            error=error,
            sent_at=sent_at,
        )
        self.db.add(log)
        await self.db.commit()
        return log


async def run_keyword_replenishment_with_db(**kwargs) -> dict[str, Any]:
    """Run keyword replenishment from a standalone worker process."""
    from app.core import database as db_module

    if db_module.async_session_factory is None:
        await db_module.init_db(create_tables=False)

    async with db_module.get_db_session() as db:
        service = AcquisitionAutomationService(db)
        return await service.replenish_keywords(**kwargs)


async def run_auto_join_with_db(**kwargs) -> dict[str, Any]:
    """Run auto-join from a standalone worker process."""
    from app.core import database as db_module

    if db_module.async_session_factory is None:
        await db_module.init_db(create_tables=False)

    async with db_module.get_db_session() as db:
        service = AcquisitionAutomationService(db)
        return await service.run_auto_join(**kwargs)


async def run_ad_delivery_with_db(**kwargs) -> dict[str, Any]:
    """Run ad delivery from a standalone worker process."""
    from app.core import database as db_module

    if db_module.async_session_factory is None:
        await db_module.init_db(create_tables=False)

    async with db_module.get_db_session() as db:
        service = AcquisitionAutomationService(db)
        return await service.run_ad_delivery(**kwargs)
