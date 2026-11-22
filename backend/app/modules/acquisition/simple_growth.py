"""Bounded growth from confirmed send results, with crash-safe attempt records.

The pool and SDK governor own Telegram sessions. Redis owns execution leases
and pacing; memberships own eligibility. Historical audits never authorize ads.
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import structlog
from sqlalchemy import case, exists, func, or_, select, update
from sqlalchemy.orm import aliased, lazyload

from app.core.account.models import AccountOperationConfig, TelegramAccount
from app.core.database_transaction_boundary import release_read_transaction
from app.core.group.identity import is_owned_group_target
from app.core.group.models import Group
from app.core.group.models import GroupAccountMembership as Member
from app.core.redis import get_redis
from app.modules.acquisition.adaptive_frequency import canonical
from app.modules.acquisition.send_result_policy import (POLICY, COUNTED, EXIT_REASON, classify, next_due)
from app.modules.acquisition.growth_policy import (
    URL,
    account_block_reason,
    manually_protected,
    policy,
)
from app.modules.acquisition.growth_results import risk_failure, risk_record
from app.modules.acquisition.member_permissions import (
    Permission,
    apply_permission,
    inspect_permission,
)
from app.modules.acquisition.models import (
    AccountAdBinding,
    AdCampaign,
    AdCreative,
    AdDeliveryLog,
    AutoJoinAttempt,
    GroupAdProfile,
)
from app.modules.acquisition.simple_limits import (
    PREFIX,
    Deferred,
    durable_outbound_used,
    lease,
    take_ad,
    take_join,
)

logger = structlog.get_logger()
STAGES = {"ads": 60, "join": 300, "review_and_verify": 60, "exit": 60}
# A chat permission failure keeps at least this much account spacing before the
# next ad attempt, whatever the allocator rolled back.
PERMISSION_FAILURE_SPACING_SECONDS = 600


async def release(db: Any) -> None:
    await release_read_transaction(db, allow_untracked_read_only=True)


def result(reason: str | None = None, **counts: Any) -> dict:
    return {"processed": 1, "succeeded": 0, "failed": 0, "skipped": int(bool(reason)),
            "reason": reason, "details": [], "errors": [], **counts}


def campaign_matches(campaign: AdCampaign, group: Group, now: datetime) -> bool:
    if not campaign.enabled or getattr(campaign, "delivery_policy", "growth") == "ad_only":
        return False
    if (campaign.start_at and campaign.start_at > now) or (campaign.end_at and campaign.end_at <= now):
        return False
    targets = campaign.get_target_group_ids()
    # Permission is the growth admission rule, including unrated groups.
    # Explicit campaign targets remain a deliberate operator restriction.
    return group.id in targets if targets else True


class GrowthService:
    def __init__(self, service: Any):
        self.service, self.db, self.pool = service, service.db, service.account_pool

    @asynccontextmanager
    async def client(self, account_id: int, purpose: str):
        await release(self.db)
        wrapper = await self.pool.acquire_by_id(account_id, purpose=purpose, raise_on_lease_failure=True)
        if wrapper is None or wrapper.client is None:
            raise Deferred("session_owner_unavailable", 30)
        try:
            yield wrapper
        finally:
            await self.pool.release(wrapper)

    async def account(self, account_id: int) -> tuple[Any, Any]:
        account = await self.db.get(TelegramAccount, account_id, populate_existing=True)
        config = await self.db.scalar(select(AccountOperationConfig).where(
            AccountOperationConfig.account_id == account_id).execution_options(populate_existing=True))
        reason = account_block_reason(account, datetime.utcnow())
        from app.core.settings_models import SystemSetting
        if account_id in {33, 34, 35} and config and config.enabled:
            pilot = await self.db.get(SystemSetting, f"growth.canary.account.{account_id}")
            if not pilot or not json.loads(pilot.value).get("send_result_started_at"):
                raise Deferred("observation_activation_required")
        pause = await self.db.get(SystemSetting, f"growth.diagnostic_pause.account.{account_id}")
        if pause:
            marker = json.loads(pause.value)
            cleared = account and account.spam_check_status == "clear" and account.spam_checked_at and account.spam_checked_at > datetime.fromisoformat(marker["at"])
            if not cleared and marker.get("until") and datetime.fromisoformat(marker["until"]) > datetime.utcnow():
                raise Deferred("account_diagnostic_pause")
        if reason or not config or not config.enabled:
            raise Deferred(reason or "account_operation_paused")
        if config.operation_mode == "ad_only":
            raise Deferred("ad_only_disabled")
        rules = await policy(self.db)
        if (rules.get("rollout_accounts") or {}).get(str(account_id), {}).get("phase") == "paused":
            raise Deferred("manual_business_pause")
        from app.modules.acquisition.join_budget import JoinRequestBudgetService
        if str(getattr(account.account_type, "value", account.account_type)) != "promoter":
            raise Deferred("promotion_account_required")
        if await JoinRequestBudgetService(self.db)._is_owned_group_owner(account_id):
            raise Deferred("owned_group_owner_protected")
        from app.core.account.age import account_age_eligibility_reason
        if reason := account_age_eligibility_reason(account, datetime.utcnow()):
            raise Deferred(reason)
        return account, config

    async def protected(self, member: Member, group: Group) -> bool:
        config = await policy(self.db, fresh=True)
        profile = await self.db.scalar(select(GroupAdProfile).where(
            GroupAdProfile.group_id == group.id).execution_options(populate_existing=True))
        manual_forbidden = bool(profile and (
            profile.blocked_reason == "manual_ad_policy_forbidden" or
            (profile.ad_policy_source == "manual" and profile.ad_policy_mode in {"forbidden", "approval_required"})))
        return bool(member.ad_status == "paused" or manually_protected(config, group, member)
            or manual_forbidden
            or await is_owned_group_target(self.db, core_group_id=group.id, telegram_group_id=group.group_id))

    async def execute(self, stage: str, account_id: int) -> dict:
        try:
            redis = await get_redis()
            await release(self.db)
            async with lease(redis, f"account:{account_id}"):
                await self.recover_results(account_id)
                await self.recover_interrupted_ads(account_id)
                from app.modules.acquisition.send_result_observation import observe
                await observe(self.db, account_id)
                await self.account(account_id)
                if stage == "ads":
                    return await self.ads(account_id)
                if stage == "join":
                    return await self.join(account_id)
                if stage == "review_and_verify":
                    return await self.review(account_id)
                if stage == "exit":
                    return await self.maintenance(account_id)
                return result("retired_stage")
        except Deferred as exc:
            return result(exc.reason, retry_after_seconds=exc.retry_after_seconds)

    async def verify(self, wrapper: Any, entity: Any, member: Member, fact: Permission,
                     options: Any) -> bool:
        """Reuse bounded local challenge handling, only for prompts addressed to us."""
        from app.modules.acquisition.qualification_verification import targeted_recent_prompt
        if member.verification_status == "complete":
            return True
        if not member.joined_at:
            return False
        redis = await get_redis()
        completed_key = PREFIX + f"verified:{member.id}:{member.joined_at.isoformat()}"
        if fact.writable is True and await redis.get(completed_key) == "completed":
            return True
        if member.joined_at < datetime.utcnow() - timedelta(hours=48):
            return member.verification_status != "pending"
        client = wrapper.client
        me = await client.get_me()
        messages = await client.get_messages(entity, limit=10)
        for message in messages:
            if not targeted_recent_prompt(message, me, member.joined_at, datetime.utcnow()):
                continue
            sender = getattr(message, "sender_id", None)
            if not isinstance(sender, int) or sender <= 0:
                continue
            rights = await client.get_permissions(entity, sender)
            if not (getattr(rights, "is_admin", False) or getattr(rights, "is_creator", False)):
                continue
            if not options.enabled:
                return False
            decision = self.service._local_join_verification_decision([message],
                can_send_messages=fact.writable, permission_reason=fact.reason, settings_config=options)
            if decision.action not in {"click_button", "send_answer", "second_hop"}:
                return False
            redis = await get_redis()
            key = PREFIX + f"verification:{member.id}:{member.joined_at.isoformat()}:{message.id}"
            if not await redis.set(key, "attempted", nx=True, ex=172800):
                return await redis.get(key) == "completed" and fact.writable is True
            outcome = await self.service._execute_join_verification_decision(
                client, entity, [message], decision, options)
            # A callback acknowledgement is not permission. The next quantum checks again.
            logger.info("growth_verification_result", membership_id=member.id,
                        success=outcome.success, reason=outcome.reason)
            if outcome.success:
                await redis.set(key, "completed", ex=172800)
                await redis.set(completed_key, "completed", ex=172800)
            return False
        # Absence of a prompt is not proof that a previously observed challenge passed.
        return member.verification_status != "pending"

    async def review(self, account_id: int, limit: int = 8) -> dict:
        from app.modules.acquisition.qualification_identity import entity_identity, peer_identity

        now = datetime.utcnow()
        controls = await policy(self.db, fresh=True)
        review_enabled = controls.get("execute_reviews", True)
        verify_enabled = controls.get("execute_verification", False)
        if not controls.get("enabled") or not (review_enabled or verify_enabled):
            return result("permission_review_paused")
        query = select(Member).options(lazyload("*")).where(
            Member.account_id == account_id, Member.status == "joined",
            Member.ad_status != "paused", Member.leave_requested_at.is_(None),
            Member.review_status == "pending",
            or_(Member.review_next_at.is_(None), Member.review_next_at <= now),
        ).order_by(Member.review_next_at.asc().nullsfirst(),
                   case((Member.verification_status == "pending", 0), else_=1), Member.id).limit(limit)
        if not review_enabled:
            query = query.where(Member.verification_status == "pending")
        members = list((await self.db.scalars(query)).all())
        options = await self.service._join_verification_settings()
        options.enabled = options.enabled and verify_enabled
        checked = approved = 0
        start_time = time.monotonic()
        for member in members:
            # Yield before the 60-second session_exec stage budget expires
            if time.monotonic() - start_time > 40.0:
                break
            group = await self.db.get(Group, member.group_id)
            if group is None:
                continue
            if await self.protected(member, group):
                member.review_status, member.ad_status = "rejected", "blocked"
                member.permission_reason, member.review_next_at = "manual_or_owned_protection", now + timedelta(days=1)
                await self.db.commit()
                continue
            member_id, generation, target = member.id, member.joined_at, group.group_id
            checked_generation = member.permission_checked_at
            complete = False
            marked_target = None
            try:
                async with self.client(account_id, "growth_permission") as wrapper:
                    entity = await wrapper.client.get_entity(target)
                    actual, expected = entity_identity(entity), peer_identity(target)
                    if (not actual or not expected or actual[0] != expected[0]
                            or (expected[1] and actual[1] != expected[1])):
                        raise Deferred("permission_identity_unconfirmed", 600)
                    marked_target = canonical(target, {"group_type": actual[1] if actual[1] == "channel" else "basic_group"})
                    permissions = await wrapper.client.get_permissions(entity, "me")
                    fact = inspect_permission(entity, permissions, datetime.utcnow())
                    if fact.member is True:
                        complete = await self.verify(wrapper, entity, member, fact, options)
                    if not complete and fact.member is True:
                        attempts = int(member.review_attempts or 0) + 1
                        delay = min(1800, 60 * 2 ** min(attempts - 1, 5))
                        fact = Permission(True, fact.writable, "verification_pending", now + timedelta(seconds=delay))
            except Exception as exc:
                exc_type = type(exc).__name__
                if exc_type == "UserNotParticipantError":
                    fact = Permission(False, False, "not_member")
                else:
                    delay = max(60, int(getattr(exc, "seconds", getattr(exc, "retry_after_seconds", 600))))
                    fact = Permission(None, None, getattr(exc, "reason", exc_type), now + timedelta(seconds=delay))
            current = await self.db.get(Member, member_id, populate_existing=True)
            if (current and current.status == "joined" and current.joined_at == generation
                    and current.permission_checked_at == checked_generation and current.ad_status != "paused"):
                if marked_target is not None:
                    current.telegram_group_id = marked_target
                if complete:
                    current.verification_status = "complete"
                    current.review_attempts = 0
                elif fact.reason == "verification_pending":
                    current.verification_status = "pending"
                    current.review_attempts = int(current.review_attempts or 0) + 1
                apply_permission(current, fact, datetime.utcnow())
                checked += 1
                approved += int(current.review_status == "approved")
            await self.db.commit()
        return result(checked=checked, approved=approved, succeeded=approved)

    async def join_link(self, account_id: int, request_token: str) -> dict:
        """One owner-delivered manual request; Redis GETDEL prevents task replay."""
        import hashlib

        from telethon.utils import get_peer_id

        from app.core.account.telegram_execution import (
            TelegramExecutionService,
            TelegramJoinRequestPendingError,
            parse_telegram_group_link,
        )
        from app.core.automation_settings import get_auto_join_scheduler_settings
        from app.core.group.identity import telegram_group_id_aliases
        from app.modules.acquisition.join_budget import JoinRequestBudgetService
        from app.modules.acquisition.join_claims import group_occupied

        await self.recover_results(account_id)
        redis = await get_redis()
        raw = await redis.getdel(PREFIX + f"manual_join:{account_id}:{request_token}")
        if raw is None:
            return result("manual_join_expired_or_consumed")
        link = json.loads(raw)["link"]
        parsed = parse_telegram_group_link(link)
        account, config = await self.account(account_id)
        scheduler = await get_auto_join_scheduler_settings(self.db)
        reason = await JoinRequestBudgetService(self.db).eligibility_reason(account, config, datetime.utcnow(),
            require_auto_join_enabled=False, global_enabled=scheduler["enabled"])
        count = await self.db.scalar(select(func.count(Member.id)).where(
            Member.account_id == account_id, Member.status.in_(["joined", "pending", "leave_failed"])))
        if reason or config.operation_mode == "ad_only" or count >= min(300, config.max_groups_total):
            return result(reason or "join_not_allowed")
        target_key = hashlib.sha256((parsed.kind + ":" + parsed.target).encode()).hexdigest()
        await release(self.db)
        async with lease(redis, f"account:{account_id}"), lease(redis, f"manual-target:{target_key}"), AsyncExitStack() as stack:
            attempted, due, resolved, error, failure = False, None, None, None, None
            preview_data = None
            cancelled = False

            async def validate(entity, **_kwargs):
                nonlocal preview_data
                if entity is None:
                    raise Deferred("private_invite_identity_unavailable")
                target = get_peer_id(entity)
                aliases = telegram_group_id_aliases(target)
                await stack.enter_async_context(lease(redis, f"join-target:{canonical(target)}"))
                group = await self.db.scalar(select(Group).where(Group.group_id.in_(aliases)).limit(1))
                if await is_owned_group_target(self.db, telegram_group_id=target):
                    raise Deferred("owned_group_protected")
                if group and await self.db.scalar(select(Member.id).where(
                        Member.account_id == account_id, Member.group_id == group.id,
                        Member.leave_error == EXIT_REASON).limit(1)):
                    raise Deferred("automatic_rejoin_blocked")
                if group and (await self.protected(Member(), group) or await self.db.scalar(
                        select(Group.id).where(Group.id == group.id, group_occupied(datetime.utcnow())))):
                    raise Deferred("join_target_protected_or_occupied")
                preview_data = {"id": target, "title": getattr(entity, "title", None),
                                "username": getattr(entity, "username", None)}
                await release(self.db)

            execution = TelegramExecutionService()
            execution._validate_qualification_join = validate

            async def enter():
                nonlocal attempted, due
                due = await self.admit_join(account_id, preview_data["id"], require_auto=False)
                attempted = True

            try:
                async with self.client(account_id, "manual_group_link_join") as wrapper:
                    resolved = await execution.join_group_by_link(
                        wrapper, link, on_join_request_attempted=enter)
                state = "joined"
            except asyncio.CancelledError:
                state, error, cancelled = "unknown", "operation_cancelled", True
            except TelegramJoinRequestPendingError:
                state = "pending"
            except Deferred:
                raise
            except Exception as exc:
                failure = exc
                error = type(exc).__name__
                state = "pending" if error == "InviteRequestSentError" else "unknown" if attempted else "failed"
            data = resolved or preview_data
            saved = await self.save_result({
                'kind': "join",
                'account_id': account_id,
                'attempt': uuid4().hex,
                'key': int(data["id"]) if data else None,
                'group_id': None,
                'title': data.get("title") if data else None,
                'username': data.get("username") if data else None,
                'state': state,
                'error': error,
                'attempted': attempted,
                'manual': True,
                'sent': datetime.utcnow().isoformat(),
                'due': due.isoformat() if due else None,
                'failure': risk_record(failure, "join"),
            })
            if cancelled:
                raise asyncio.CancelledError()
            return saved

    async def join(self, account_id: int, *, dry_run: bool = False) -> dict:
        from telethon.tl.functions.channels import JoinChannelRequest
        from telethon.utils import get_peer_id

        from app.core.automation_settings import get_auto_join_scheduler_settings
        from app.modules.acquisition.join_budget import JoinRequestBudgetService
        from app.modules.acquisition.join_claims import group_occupied
        from app.modules.acquisition.qualification_identity import entity_identity, peer_identity

        now = datetime.utcnow()
        account, config = await self.account(account_id)
        scheduler = await get_auto_join_scheduler_settings(self.db)
        reason = await JoinRequestBudgetService(self.db).eligibility_reason(account, config, now,
            require_auto_join_enabled=True, global_enabled=bool(scheduler["enabled"]))
        if reason:
            return result(reason)
        total = await self.db.scalar(select(func.count(Member.id)).where(
            Member.account_id == account_id, Member.status.in_(["joined", "pending", "leave_failed"])))
        if total >= min(300, config.max_groups_total):
            return result("group_capacity")
        await release(self.db)
        redis = await get_redis()
        due = await redis.get(PREFIX + f"join:{account_id}:due")
        if due and float(due) > now.replace(tzinfo=UTC).timestamp():
            return result("join_cooldown")
        cursor_key = PREFIX + f"join:{account_id}:candidate_offset"
        offset = max(0, int(await redis.get(cursor_key) or 0))
        query = select(Group).options(lazyload("*")).where(
            Group.status.in_(["pending", "pending_join", "cooling_down"]),
            Group.username.is_not(None), ~group_occupied(now),
        ).order_by(Group.level_score.desc(), Group.updated_at, Group.id).limit(30)
        candidates = list((await self.db.scalars(query.offset(offset))).all())
        if not candidates and offset:
            offset = 0
            candidates = list((await self.db.scalars(query)).all())
        if not candidates:
            keywords = await self.service._get_search_keywords(config, limit=1)
            if not keywords:
                replenish_res = await self.service._ensure_keywords_for_auto_join(
                    config, current_keywords=[], target_count=1
                )
                if replenish_res:
                    keywords = replenish_res.get("keywords") or []
                if not keywords:
                    keywords = await self.service._get_search_keywords(config, limit=1)
            if not keywords:
                return result("no_keywords_or_queue")
            if dry_run:
                return result("search_required")
            keyword = keywords[0]
            text = keyword.text
            await release(self.db)
            groups = await self.service.group_finder.search_by_keyword(
                text, limit=10, account_id=account_id, raise_errors=True, purpose="auto_join_search")
            filtered, removed = await self.service._filter_groups_by_title_blacklist(
                account_id, groups, source_keyword=text)
            await self.service._persist_search_results_for_queue(account_id, text, groups,
                title_filtered_ids={item["telegram_group_id"] for item in removed})
            await self.service._record_search_keyword_feedback(keyword, found_count=len(groups),
                                                               candidate_count=len(filtered))
            return result(discovered=len(filtered))
        filters = await self.service._search_filter_settings()
        candidate = None
        examined = 0
        for item in candidates:
            examined += 1
            if self.service._title_blacklist_match(item.title or "", filters) or (
                    filters.cjk_title_required and not self.service._title_has_cjk(item.title)):
                continue
            if await is_owned_group_target(self.db, core_group_id=item.id, telegram_group_id=item.group_id):
                continue
            candidate = item
            break
        await release(self.db)
        if not dry_run:
            await redis.set(cursor_key, offset + examined if len(candidates) == 30 or candidate else 0)
        if candidate is None:
            return result("candidate_window_filtered", examined=examined)
        if dry_run:
            return result(candidate_id=candidate.id)
        group_id, target, username = candidate.id, candidate.group_id, candidate.username
        await release(self.db)
        initial_key = canonical(target)
        async with lease(redis, f"join-target:{initial_key if initial_key is not None else 'legacy:' + str(target)}"), AsyncExitStack() as stack:
            occupied = await self.db.scalar(select(Group.id).where(Group.id == group_id, group_occupied(now)))
            if occupied:
                return result("join_target_occupied")
            attempted = False
            state, error, next_join, failure = "failed", None, None, None
            cancelled = False
            async with self.client(account_id, "auto_join") as wrapper:
                try:
                    entity = await wrapper.client.get_entity(username)
                except Exception as exc:
                    candidate_group = await self.db.get(Group, group_id)
                    if candidate_group:
                        candidate_group.status = "join_failed"
                        candidate_group.updated_at = now
                        await self.db.commit()
                    return result("join_target_not_found", error=type(exc).__name__)
                actual, expected = entity_identity(entity), peer_identity(target)
                if (not getattr(entity, "megagroup", False) or not actual or not expected
                        or actual[0] != expected[0] or (expected[1] and actual[1] != expected[1])):
                    candidate_group = await self.db.get(Group, group_id)
                    if candidate_group:
                        candidate_group.status = "join_failed"
                        candidate_group.updated_at = now
                        await self.db.commit()
                    return result("join_target_identity_changed")
                target = get_peer_id(entity)
                if target != initial_key:
                    await stack.enter_async_context(lease(redis, f"join-target:{target}"))
                next_join = await self.admit_join(account_id, target, require_auto=True)
                attempted = True
                try:
                    await wrapper.client(JoinChannelRequest(entity))
                    state = "joined"
                except asyncio.CancelledError:
                    state, error, cancelled = "unknown", "operation_cancelled", True
                except Exception as exc:
                    failure = exc
                    error = type(exc).__name__
                    if error == "UserAlreadyParticipantError":
                        state = "joined"
                    elif error == "InviteRequestSentError":
                        state = "pending"
                    else:
                        state = "unknown" if isinstance(exc, (TimeoutError, OSError)) else "failed"
            saved = await self.save_result({
                'kind': "join",
                'account_id': account_id,
                'attempt': uuid4().hex,
                'key': target,
                'group_id': group_id,
                'username': username,
                'state': state,
                'error': error,
                'attempted': attempted,
                'manual': False,
                'sent': now.isoformat(),
                'due': next_join.isoformat() if next_join else None,
                'failure': risk_record(failure, "join"),
            })
            if cancelled:
                raise asyncio.CancelledError()
            return saved

    async def admit_join(self, account_id: int, target: int, *, require_auto: bool) -> datetime:
        """Recheck mutable controls after entity resolution, immediately before debit/RPC."""
        from app.core.automation_settings import get_auto_join_scheduler_settings
        from app.core.group.identity import telegram_group_id_aliases
        from app.modules.acquisition.join_budget import JOIN_REQUEST_HARD_CAP, JoinRequestBudgetService
        from app.modules.acquisition.join_claims import group_occupied
        now = datetime.utcnow()
        account, config = await self.account(account_id)
        scheduler = await get_auto_join_scheduler_settings(self.db)
        reason = await JoinRequestBudgetService(self.db).eligibility_reason(account, config, now,
            require_auto_join_enabled=require_auto, global_enabled=scheduler["enabled"])
        count = await self.db.scalar(select(func.count(Member.id)).where(
            Member.account_id == account_id, Member.status.in_(["joined", "pending", "leave_failed"])))
        group = await self.db.scalar(select(Group).where(Group.group_id.in_(telegram_group_id_aliases(target))))
        if reason or count >= min(300, config.max_groups_total):
            raise Deferred(reason or "group_capacity")
        if config.next_join_after and config.next_join_after > now:
            raise Deferred("join_cooldown", int((config.next_join_after - now).total_seconds()))
        if group and await self.db.scalar(select(Member.id).where(
                Member.account_id == account_id, Member.group_id == group.id,
                Member.leave_error == EXIT_REASON).limit(1)):
            raise Deferred("automatic_rejoin_blocked")
        if await is_owned_group_target(self.db, telegram_group_id=target):
            raise Deferred("owned_group_protected")
        if group and (await self.protected(Member(), group) or await self.db.scalar(
                select(Group.id).where(Group.id == group.id, group_occupied(now)))):
            raise Deferred("join_target_protected_or_occupied")
        recovering = config.business_stage == "cooldown" or account.risk_level == "watch" or bool(
            account.risk_recovery_until and account.risk_recovery_until > now)
        used = await self.db.scalar(select(func.count(AutoJoinAttempt.id)).where(
            AutoJoinAttempt.account_id == account_id, AutoJoinAttempt.telegram_action_attempted.is_(True),
            AutoJoinAttempt.attempted_at >= now.replace(hour=0, minute=0, second=0, microsecond=0)))
        from app.core.account.outbound_budget import effective_capacity_limits
        limits = await effective_capacity_limits(self.db, account, config, now)
        join_cap = min(JOIN_REQUEST_HARD_CAP, limits["join"], max(0, int(config.max_groups_per_day or 0)))
        join_interval = max(7200 if recovering else 2880, limits["join_interval_seconds"],
                            int(config.join_interval_min_seconds or 0))
        await release(self.db)
        return await take_join(await get_redis(), account_id, recovering=recovering,
                               durable_used=used, cap=join_cap, interval=join_interval)

    async def persist_join_result(self, data: dict) -> dict:
        from app.core.group.identity import telegram_group_id_aliases
        now = datetime.fromisoformat(data["sent"])
        account_id, target, state = data["account_id"], data["key"], data["state"]
        for retry in range(3):
            try:
                if await self.db.scalar(select(AutoJoinAttempt.id).where(
                        AutoJoinAttempt.reservation_key == data["attempt"])):
                    return result(succeeded=int(state == "joined"), outcome=state)
                group = await self.db.get(Group, data["group_id"]) if data["group_id"] else None
                if group is None and target is not None:
                    group = await self.db.scalar(select(Group).where(
                        Group.group_id.in_(telegram_group_id_aliases(target))).limit(1))
                    if group is None:
                        group = Group(group_id=target, title=data.get("title"), username=data.get("username"), status="pending_join")
                        self.db.add(group)
                        await self.db.flush()
                if group and state in {"joined", "pending"}:
                    member = await self.db.scalar(select(Member).where(
                        Member.account_id == account_id, Member.group_id == group.id))
                    if member is None:
                        member = Member(account_id=account_id, group_id=group.id, telegram_group_id=target)
                        self.db.add(member)
                    # A newer listener fact wins over a delayed result projection.
                    if not member.permission_checked_at or member.permission_checked_at <= now:
                        paused = member.ad_status == "paused"
                        member.telegram_group_id = target
                        member.status, member.joined_at = state, now if state == "joined" else None
                        member.left_at, member.review_status = None, "pending"
                        member.ad_status = "paused" if paused else "blocked"
                        member.verification_status, member.review_next_at = "unknown", now
                        member.can_send_text, member.permission_checked_at = None, None
                        member.leave_requested_at = member.leave_retry_at = member.leave_error = None
                    group.status = "active"
                elif group and not data["manual"]:
                    group.status, group.updated_at = "join_failed", now
                config = await self.db.scalar(select(AccountOperationConfig).where(
                    AccountOperationConfig.account_id == account_id))
                if config and data["due"]:
                    config.next_join_after = max(config.next_join_after or now, datetime.fromisoformat(data["due"]))
                self.db.add(AutoJoinAttempt(account_id=account_id, group_id=group.id if group else None,
                    telegram_group_id=target, group_username=data.get("username"),
                    group_title=group.title if group else None, source_keyword=group.source_keyword if group else None,
                    reservation_key=data["attempt"], status="success" if state == "joined" else state,
                    reason=("manual_link_" if data["manual"] else "") + state, error=data["error"],
                    telegram_action_attempted=data["attempted"], request_state="completed", reconciliation_status="disabled",
                    request_sent_at=now if data["attempted"] else None, attempted_at=now,
                    joined_at=now if state == "joined" else None))
                await risk_failure(self.db, account_id, "join", data["failure"], target or 0, now)
                await self.db.commit()
                return result(succeeded=int(state == "joined"), joined=int(state == "joined"), outcome=state)
            except Exception:
                await self.db.rollback()
                if retry == 2:
                    raise
                await asyncio.sleep(0.1 * (retry + 1))

    async def ads(self, account_id: int) -> dict:
        from app.core.automation_settings import get_ad_delivery_execution_settings
        now = datetime.utcnow()
        account, config = await self.account(account_id)
        if not config.auto_ads_enabled or not (await get_ad_delivery_execution_settings(self.db))["enabled"]:
            return result("ad_delivery_paused")
        redis = await get_redis()
        # The account cooldown is global: checking it here keeps a cooling
        # account from paying for candidate queries, client preparation and a
        # full target sweep that would end at the same cooldown anyway.
        due = await redis.get(PREFIX + f"ads:{account_id}:due")
        if due:
            wait = float(due) - now.replace(tzinfo=UTC).timestamp()
            if wait > 0:
                return result("account_cooldown", retry_after_seconds=int(wait) + 1)
        other_member = aliased(Member)
        rows = (await self.db.execute(select(Member, Group, AdCampaign, AccountAdBinding)
            .join(Group, Group.id == Member.group_id)
            .join(AccountAdBinding, AccountAdBinding.account_id == Member.account_id)
            .join(AdCampaign, AdCampaign.id == AccountAdBinding.ad_campaign_id)
            .options(lazyload("*"))
            .where(Member.account_id == account_id, Member.status == "joined", Member.left_at.is_(None),
                Member.review_status == "approved", Member.ad_status == "active",
                Member.verification_status == "complete",
                Member.next_ad_allowed_at <= now, Member.leave_requested_at.is_(None),
                ~exists().where(other_member.group_id == Member.group_id,
                                other_member.next_ad_allowed_at > now),
                or_(Member.ad_pause_until.is_(None), Member.ad_pause_until <= now),
                AccountAdBinding.enabled.is_(True), AdCampaign.enabled.is_(True),
                AdCampaign.delivery_policy != "ad_only",
                or_(AdCampaign.start_at.is_(None), AdCampaign.start_at <= now),
                or_(AdCampaign.end_at.is_(None), AdCampaign.end_at > now))
            .order_by(Member.next_ad_allowed_at, Member.id.desc(), AccountAdBinding.priority.desc()).limit(100))).all()
        examined = set()
        for member, group, campaign, _binding in rows:
            pair = (member.id, campaign.id)
            if pair in examined:
                continue
            examined.add(pair)
            if not campaign_matches(campaign, group, now) or await self.protected(member, group):
                continue
            if group.ad_delivery_account_id and group.ad_delivery_account_id != account_id:
                continue
            creative_query = select(AdCreative).where(AdCreative.enabled.is_(True))
            allowed_binding = select(AccountAdBinding.id).where(
                AccountAdBinding.account_id == account_id,
                AccountAdBinding.ad_campaign_id == campaign.id,
                AccountAdBinding.enabled.is_(True),
                or_(AccountAdBinding.creative_id.is_(None), AccountAdBinding.creative_id == AdCreative.id))
            creative_query = creative_query.where(allowed_binding.exists())
            creatives = list((await self.db.scalars(creative_query)).all())
            creatives = [c for c in creatives if c.content.strip() and not c.media_url and not c.link_url
                         and not URL.search(c.content) and c.creative_type == "text"]
            if not creatives:
                continue
            creative = self.service._weighted_creative_choice(creatives)
            key = canonical(member.telegram_group_id)
            if key is None:
                # Old positive IDs have no chat/channel namespace. Schedule a
                # live permission/identity check instead of guessing a target.
                member.review_status, member.review_next_at = "pending", now
                member.can_send_text, member.permission_reason = None, "permission_identity_unconfirmed"
                await self.db.commit()
                continue
            try:
                sent = await self.send(account_id, member.id, campaign.id, creative.id, key)
                if sent.get("succeeded", 0) > 0:
                    return sent
                if sent.get("outcome") == "permission_changed":
                    # One chat-level permission failure must not fund an
                    # immediate burst against other groups. The daily count is
                    # still rolled back (a dead group consumes no quota), but
                    # the account is held at the configured minimum spacing and
                    # this stage stops; the group is requeued for review by the
                    # persisted result.
                    return sent
                if sent.get("reason") not in (
                    "group_send_interval", "frequency_paused", "permission_refresh_required",
                    "final_gate_denied", "text_profile_required", "delivery_target_removed",
                    "invalid_group_identity", "group_daily_cap"
                ):
                    return sent
            except Deferred as exc:
                if exc.reason == "account_cooldown":
                    # The wait is account-wide: no other target can run sooner.
                    return result(exc.reason, retry_after_seconds=exc.retry_after_seconds)
                if exc.reason == "group_cooldown":
                    continue
                raise
        return result("no_due_eligible_target")

    async def _permission_retry_spacing(self, account: Any, config: Any, now: datetime) -> int:
        """Minimum account spacing kept after a chat permission failure."""
        from app.core.account.outbound_budget import effective_capacity_limits
        from app.core.automation_settings import get_ad_delivery_throttle_settings
        limits = await effective_capacity_limits(self.db, account, config, now)
        throttle = await get_ad_delivery_throttle_settings(self.db)
        return int(max(limits["ad_interval_seconds"],
                       throttle["growth_min_interval_seconds"] if throttle["enabled"] else 0,
                       PERMISSION_FAILURE_SPACING_SECONDS))

    async def send(self, account_id: int, member_id: int, campaign_id: int, creative_id: int, key: int) -> dict:
        key = canonical(key)
        if key is None:
            return result("invalid_group_identity")
        await release(self.db)
        redis = await get_redis()
        await self.recover_results(account_id)
        async with lease(redis, f"group:{key}"):
            async with self.client(account_id, "ad_delivery") as wrapper:
                try:
                    entity = await wrapper.client.get_input_entity(key)
                except Exception as exc:
                    return await self.resolution_failure(account_id, member_id, campaign_id, creative_id, key, exc)
                return await self._send(account_id, member_id, campaign_id, creative_id, key, wrapper, entity)

    async def resolution_failure(self, account_id: int, member_id: int, campaign_id: int, creative_id: int, key: int, exc: Exception) -> dict:
        from app.core.automation_settings import get_ad_delivery_execution_settings
        account, config = await self.account(account_id)
        member = await self.db.get(Member, member_id, populate_existing=True)
        group = await self.db.get(Group, member.group_id) if member else None
        if not member or not group or member.status != "joined" or await self.protected(member, group):
            return result("final_gate_denied")
        settings = await get_ad_delivery_execution_settings(self.db)
        now = datetime.utcnow()
        outcome = classify(exc)
        if outcome == "unknown":
            outcome = "failed"  # Resolution failed before any advertising write was attempted.
        data = {"account_id": account_id, "member_id": member_id, "group_id": group.id,
                "campaign_id": campaign_id, "creative_id": creative_id, "key": key,
                "attempt": uuid4().hex, "generation": member.joined_at.isoformat() if member.joined_at else None,
                "sent": now.isoformat(), "gap": settings["send_failure_retry_seconds"],
                "message_id": None, "outcome": outcome, "error": type(exc).__name__,
                "retry_seconds": settings["send_failure_retry_seconds"],
                "exit_threshold": settings["send_failure_exit_threshold"], "admitted_at": None,
                "wait_seconds": max(0, int(getattr(exc, "seconds", 0))) + 1,
                "failure": risk_record(exc, "ad_delivery")}
        saved = await self.persist_ad_result(data)
        if outcome == "permission_changed":
            await release(self.db)
            redis = await get_redis()
            due = now.replace(tzinfo=UTC).timestamp() + max(600, int(config.message_interval_seconds or 0))
            previous = float(await redis.get(PREFIX + f"ads:{account_id}:due") or 0)
            await redis.set(PREFIX + f"ads:{account_id}:due", max(previous, due), ex=86400)
        return saved

    async def _send(self, account_id: int, member_id: int, campaign_id: int, creative_id: int,
                    key: int, wrapper: Any, entity: Any) -> dict:
        from app.core.automation_settings import get_ad_delivery_execution_settings, get_ad_delivery_throttle_settings
        from app.core.account.outbound_budget import effective_capacity_limits
        from app.core.operating_time import operating_day_start
        from app.core.group.identity import telegram_group_id_aliases
        now = datetime.utcnow()
        account, config = await self.account(account_id)
        member = await self.db.get(Member, member_id, populate_existing=True)
        if member is None:
            return result("delivery_target_removed")
        group = await self.db.get(Group, member.group_id, populate_existing=True)
        campaign = await self.db.get(AdCampaign, campaign_id, populate_existing=True)
        creative = await self.db.get(AdCreative, creative_id, populate_existing=True)
        binding = await self.db.scalar(select(AccountAdBinding.id).where(
            AccountAdBinding.account_id == account_id, AccountAdBinding.ad_campaign_id == campaign_id,
            or_(AccountAdBinding.creative_id == creative_id, AccountAdBinding.creative_id.is_(None)),
            AccountAdBinding.enabled.is_(True)))
        settings = await get_ad_delivery_execution_settings(self.db)
        if not group or not campaign or not creative:
            return result("delivery_target_removed")
        if (not config.auto_ads_enabled or not settings["enabled"] or not binding
                or not campaign_matches(campaign, group, now) or member.status != "joined"
                or member.review_status != "approved" or member.ad_status != "active"
                or member.verification_status != "complete"
                or member.leave_requested_at or (member.ad_pause_until and member.ad_pause_until > now)
                or (group.ad_delivery_account_id and group.ad_delivery_account_id != account_id)
                or (member.next_ad_allowed_at and member.next_ad_allowed_at > now)
                or self.service._in_quiet_hours(config, now) or await self.protected(member, group)):
            return result("final_gate_denied")
        if (not creative.enabled or creative.creative_type != "text" or not creative.content.strip()
                or creative.media_url or creative.link_url or URL.search(creative.content)):
            return result("text_profile_required")
        if campaign.send_mode == "scheduled" and self.service._scheduled_slot_start(campaign, now) is None:
            return result("scheduled_time_not_due")
        aliases = telegram_group_id_aliases(key)
        shared_due = await self.db.scalar(select(func.max(Member.next_ad_allowed_at)).where(
            Member.telegram_group_id.in_(aliases)))
        if shared_due and shared_due > now:
            return result("group_send_interval")
        counted = (AdDeliveryLog.status.in_(COUNTED), AdDeliveryLog.created_at >= operating_day_start(now))
        group_used = await self.db.scalar(select(func.count(AdDeliveryLog.id)).where(
            *counted, AdDeliveryLog.telegram_group_id.in_(aliases), AdDeliveryLog.ad_campaign_id == campaign_id)) or 0
        own_used = await self.db.scalar(select(func.count(AdDeliveryLog.id)).where(
            *counted, AdDeliveryLog.account_id == account_id, AdDeliveryLog.group_id == group.id)) or 0
        if (group_used >= campaign.max_sends_per_group_per_day
                or own_used >= member.account_group_daily_cap):
            member.next_ad_allowed_at = operating_day_start(now) + timedelta(days=1)
            await self.db.commit()
            return result("group_daily_cap")
        gap = max(int(settings["growth_group_global_cooldown_seconds"]), int(campaign.interval_minutes or 0) * 60)
        limits = await effective_capacity_limits(self.db, account, config, now)
        if limits["action_pauses"].get("ad"):
            return result("ad_flood_wait")
        cap = min(limits["ad"], campaign.max_sends_per_account_per_day, limits["total"])
        throttle = await get_ad_delivery_throttle_settings(self.db)
        account_gap = max(limits["ad_interval_seconds"],
                          throttle["growth_min_interval_seconds"] if throttle["enabled"] else 0)
        data = {"account_id": account_id, "member_id": member_id, "group_id": group.id,
                "campaign_id": campaign_id, "creative_id": creative_id, "key": key,
                "attempt": uuid4().hex, "generation": member.joined_at.isoformat() if member.joined_at else None,
                "sent": now.isoformat(), "gap": gap, "quota": 0, "message_id": None, "message_ttl": None,
                "outcome": "unknown", "error": None, "failure": None, "admitted_at": None,
                "failure_spacing": max(account_gap, PERMISSION_FAILURE_SPACING_SECONDS),
                "retry_seconds": settings["send_failure_retry_seconds"],
                "exit_threshold": settings["send_failure_exit_threshold"], "wait_seconds": 0,
                "automatic_expiry_known": False, "policy": POLICY}
        body = creative.content
        await release(self.db)
        redis = await get_redis()
        async with lease(redis, f"outbound-admission:{account_id}"):
            durable_used = await durable_outbound_used(self.db, account_id, now)
            durable_ads = await self.db.scalar(select(func.count(AdDeliveryLog.id)).where(
                AdDeliveryLog.account_id == account_id, *counted)) or 0
            await release(self.db)
            data["admitted_at"] = datetime.utcnow().isoformat()
            await take_ad(redis, account_id, key, cap=cap, account_interval=account_gap,
                          group_interval=gap, total_cap=limits["total"], durable_used=durable_used,
                          durable_ads=durable_ads, attempt_id=data["attempt"])
            try:
                current = await self.db.scalar(select(Member).options(lazyload("*")).where(
                    Member.id == member_id).execution_options(populate_existing=True).with_for_update(of=Member))
                _, final_config = await self.account(account_id)
                final_settings = await get_ad_delivery_execution_settings(self.db)
                final_group = await self.db.get(Group, data["group_id"], populate_existing=True)
                final_campaign = await self.db.get(AdCampaign, campaign_id, populate_existing=True)
                final_creative = await self.db.get(AdCreative, creative_id, populate_existing=True)
                final_binding = await self.db.scalar(select(AccountAdBinding.id).where(
                    AccountAdBinding.account_id == account_id, AccountAdBinding.ad_campaign_id == campaign_id,
                    or_(AccountAdBinding.creative_id == creative_id, AccountAdBinding.creative_id.is_(None)),
                    AccountAdBinding.enabled.is_(True)))
                if (not current or current.status != "joined" or current.ad_status != "active"
                        or current.review_status != "approved" or current.verification_status != "complete"
                        or (current.joined_at.isoformat() if current.joined_at else None) != data["generation"]
                        or current.leave_requested_at
                        or (current.ad_pause_until and current.ad_pause_until > datetime.utcnow())
                        or not final_config.auto_ads_enabled or not final_settings["enabled"]
                        or not final_binding or self.service._in_quiet_hours(final_config, datetime.utcnow())
                        or not final_group or not final_campaign
                        or not campaign_matches(final_campaign, final_group, datetime.utcnow())
                        or (final_group.ad_delivery_account_id and final_group.ad_delivery_account_id != account_id)
                        or not final_creative or not final_creative.enabled or final_creative.content != body
                        or final_creative.creative_type != "text" or final_creative.media_url or final_creative.link_url
                        or await self.protected(current, final_group)):
                    raise Deferred("final_gate_denied")
                self.db.add(AdDeliveryLog(account_id=account_id, group_id=data["group_id"],
                    telegram_group_id=key, ad_campaign_id=campaign_id, creative_id=creative_id,
                    reservation_token=data["attempt"], status="sending", created_at=now,
                    survival_status="not_required", survival_stage="complete",
                    qualification_context_json=json.dumps({"policy": POLICY, "attempt_context": data})))
                current.next_ad_allowed_at = next_due(now, gap, "unknown", data["retry_seconds"])
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                from app.modules.acquisition.simple_limits import revert_ad
                await revert_ad(redis, account_id, attempt_id=data["attempt"],
                                admitted_at=datetime.fromisoformat(data["admitted_at"]),
                                cooldown_seconds=data["failure_spacing"])
                raise
        cancelled = False
        try:
            message = await wrapper.client.send_message(entity, body, parse_mode=None, link_preview=False)
            value = getattr(message, "id", None)
            data["message_id"] = value if type(value) is int and value > 0 else None
            data["outcome"] = "success" if data["message_id"] else "unknown"
            data["error"] = None if data["message_id"] else "message_id_missing"
        except asyncio.CancelledError:
            data["error"], cancelled = "operation_cancelled", True
        except Exception as exc:
            data["outcome"], data["error"] = classify(exc), type(exc).__name__
            data["wait_seconds"] = max(0, int(getattr(exc, "seconds", 0))) + (1 if hasattr(exc, "seconds") else 0)
            data["failure"] = risk_record(exc, "ad_delivery")
            if data["outcome"] == "rate_limited" and data["error"] != "SlowModeWaitError":
                data["failure_spacing"] = max(data["failure_spacing"], data["wait_seconds"])
        saved = await self.save_result(data)
        if cancelled:
            raise asyncio.CancelledError()
        return saved

    async def save_result(self, data: dict) -> dict:
        # This journal is created AFTER the RPC; it contains results, never send intents.
        redis = await get_redis()
        journal, attempt = PREFIX + f"results:{data['account_id']}", data["attempt"]
        try:
            await redis.hset(journal, attempt, json.dumps(data))
        except Exception:
            logger.exception("growth_result_journal_unavailable", attempt=attempt, message_id=data.get("message_id"))
        saved = await (self.persist_join_result(data) if data.get("kind") == "join" else self.persist_ad_result(data))
        await self.settle_admission(data)
        await release(self.db)
        await redis.hdel(journal, attempt)
        return saved

    async def recover_results(self, account_id: int, limit: int = 4) -> None:
        await release(self.db)
        redis = await get_redis()
        journal = PREFIX + f"results:{account_id}"
        pending = await redis.hgetall(journal)
        for attempt, raw in list(pending.items())[:limit]:
            data = json.loads(raw)
            name = "join-target" if data.get("kind") == "join" else "group"
            async with lease(redis, f"{name}:{canonical(data['key'])}"):
                await (self.persist_join_result(data) if name == "join-target" else self.persist_ad_result(data))
                await self.settle_admission(data)
                await release(self.db)
                await redis.hdel(journal, attempt)
        if len(pending) > limit:
            raise Deferred("result_commit_backlog", 15)

    async def settle_admission(self, data: dict) -> None:
        """Release a definite failure once; keep unknown writes and all cooldowns."""
        if data.get("outcome") not in {"failed", "permission_changed", "content_invalid", "not_member", "rate_limited"} or not data.get("admitted_at"):
            return
        from app.modules.acquisition.simple_limits import revert_ad
        await release(self.db)
        await revert_ad(await get_redis(), data["account_id"], attempt_id=data["attempt"],
                        admitted_at=datetime.fromisoformat(data["admitted_at"]),
                        cooldown_seconds=max(PERMISSION_FAILURE_SPACING_SECONDS,
                                             int(data.get("failure_spacing") or 0)))
        if data["outcome"] == "permission_changed":
            from app.modules.acquisition.growth_controls import pause_repeated_permission_failures
            await pause_repeated_permission_failures(self.db, data["account_id"], datetime.utcnow())

    async def persist_ad_result(self, data: dict) -> dict:
        sent = datetime.fromisoformat(data["sent"])
        generation = datetime.fromisoformat(data["generation"]) if data.get("generation") else None
        outcome, message_id = data["outcome"], data.get("message_id")
        for retry in range(3):
            try:
                log = await self.db.scalar(select(AdDeliveryLog).options(lazyload("*")).where(
                    AdDeliveryLog.reservation_token == data["attempt"]).with_for_update(of=AdDeliveryLog))
                if log is not None and log.status != "sending":
                    return result(succeeded=int(log.status == "success"), message_id=log.telegram_message_id,
                                  outcome=log.status)
                if log is None:
                    log = AdDeliveryLog(account_id=data["account_id"], group_id=data["group_id"],
                        telegram_group_id=data["key"], ad_campaign_id=data["campaign_id"],
                        creative_id=data["creative_id"], reservation_token=data["attempt"], created_at=sent)
                    self.db.add(log)
                log.status, log.error = outcome, data.get("error")
                log.telegram_message_id, log.sent_at = message_id, sent if message_id else None
                log.survival_status, log.survival_stage, log.survival_check_due_at = "not_required", "complete", None
                log.qualification_context_json = json.dumps({"policy": POLICY, "membership_joined_at": data.get("generation"),
                    "attempt_context": data, "automatic_resend": False})
                await self.db.flush()
                current = await self.db.scalar(select(Member).options(lazyload("*")).where(Member.id == data["member_id"]).with_for_update(of=Member))
                newer = await self.db.scalar(select(AdDeliveryLog.id).where(
                    AdDeliveryLog.account_id == data["account_id"], AdDeliveryLog.group_id == data["group_id"],
                    AdDeliveryLog.created_at > sent).limit(1))
                if current and current.joined_at == generation and not newer and current.status == "joined":
                    current.next_ad_allowed_at = max(current.next_ad_allowed_at or sent,
                        next_due(sent, data["gap"], outcome, data.get("retry_seconds", 1800), data.get("wait_seconds", 0)))
                    if outcome == "success":
                        current.ad_failure_streak = 0
                        if current.ad_status != "paused":
                            current.can_send_text, current.permission_reason = True, "send_confirmed"
                    elif outcome == "permission_changed" and current.ad_status != "paused":
                        current.ad_failure_streak = int(current.ad_failure_streak or 0) + 1
                        current.permission_reason = data.get("error")
                        if current.ad_failure_streak >= data.get("exit_threshold", 3):
                            current.leave_requested_at, current.leave_retry_at = sent, sent
                            current.leave_error, current.leave_attempts = EXIT_REASON, 0
                            current.ad_status = "blocked"
                    elif outcome == "not_member":
                        current.status, current.left_at = "left", sent
                        current.ad_status, current.review_status = "blocked", "rejected"
                    elif outcome == "content_invalid":
                        creative = await self.db.get(AdCreative, data["creative_id"])
                        if creative:
                            creative.enabled = False
                await risk_failure(self.db, data["account_id"], "ad_delivery", data.get("failure"), data["key"], sent)
                await self.db.commit()
                return result(succeeded=int(outcome == "success"), message_id=message_id, outcome=outcome,
                              failed=int(outcome in {"failed", "permission_changed", "content_invalid", "not_member"}),
                              unknown=int(outcome == "unknown"))
            except Exception:
                await self.db.rollback()
                if retry == 2:
                    raise
                await asyncio.sleep(0.1 * (retry + 1))

    async def recover_interrupted_ads(self, account_id: int) -> int:
        # Caller owns the account lease. No active request can be replayed here.
        rows = list((await self.db.scalars(select(AdDeliveryLog).where(
            AdDeliveryLog.account_id == account_id, AdDeliveryLog.status == "sending",
            AdDeliveryLog.qualification_context_json.contains(POLICY)
        ).order_by(AdDeliveryLog.created_at, AdDeliveryLog.id).limit(20))).all())
        recovered = 0
        for log in rows:
            context = json.loads(log.qualification_context_json or "{}")
            data = context.get("attempt_context")
            if context.get("policy") != POLICY or not data:
                continue
            data.update(outcome="unknown", error="interrupted_without_receipt")
            await self.persist_ad_result(data)
            recovered += 1
        return recovered

    async def maintenance(self, account_id: int, limit: int = 4) -> dict:
        exited = await self.exits(account_id, limit)
        return result(exited=exited, succeeded=0)

    async def exits(self, account_id: int, limit: int) -> int:
        from telethon.tl.functions.channels import LeaveChannelRequest
        from telethon.tl.functions.messages import DeleteChatUserRequest
        from telethon.tl.types import Chat, InputUserSelf
        now = datetime.utcnow()
        controls = await policy(self.db, fresh=True)
        if not controls.get("enabled") or not controls.get("execute_exits"):
            return 0
        members = list((await self.db.scalars(select(Member).where(Member.account_id == account_id,
            Member.status.in_(["joined", "leave_failed"]), Member.leave_requested_at.is_not(None),
            Member.leave_retry_at <= now, Member.leave_attempts < 3,
            Member.leave_error.in_([EXIT_REASON, "manual", "manual_exit", "operator_requested"]),
        ).order_by(Member.leave_retry_at).limit(limit))).all())
        exited = 0
        for member in members:
            group = await self.db.get(Group, member.group_id)
            if await self.protected(member, group):
                continue
            member_id, generation, target = member.id, member.joined_at, group.group_id
            error, wait = None, 0
            try:
                async with self.client(account_id, "growth_exit") as wrapper:
                    entity = await wrapper.client.get_entity(target)
                    request = DeleteChatUserRequest(entity.id, InputUserSelf()) if isinstance(entity, Chat) else LeaveChannelRequest(entity)
                    await wrapper.client(request)
            except Exception as exc:
                error = None if type(exc).__name__ == "UserNotParticipantError" else type(exc).__name__
                if type(exc).__name__ in {"FloodWaitError", "SlowModeWaitError"}:
                    wait = max(1, int(exc.seconds)) + 1
            current = await self.db.get(Member, member_id, populate_existing=True)
            if current is None or current.joined_at != generation:
                continue
            current.ad_status = "paused" if current.ad_status == "paused" else "blocked"
            current.review_status = "rejected"
            if wait:
                current.leave_retry_at = now + timedelta(seconds=wait)
            elif error:
                current.leave_attempts += 1
                current.status = "leave_failed"
                current.leave_retry_at = (now + timedelta(minutes=(10 if current.leave_attempts == 1 else 30))
                                         if current.leave_attempts < 3 else None)
            else:
                current.status, current.left_at, current.leave_confirmed_at = "left", now, now
                current.leave_retry_at = None
                await self.db.execute(update(Group).where(
                    Group.id == current.group_id, Group.ad_delivery_account_id == account_id,
                ).values(ad_delivery_account_id=None).execution_options(synchronize_session=False))
                exited += 1
            await self.db.commit()
        return exited
