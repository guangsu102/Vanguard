"""
Coupon Distributor

Distributes coupons and rewards to users.
"""

import asyncio
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import structlog
from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.campaign.models import Campaign
from app.core.config import settings
from app.integrations.sub2api import Sub2APIClient, Sub2APIError, get_sub2api_client
from app.modules.guardian.models import CouponDistribution

logger = structlog.get_logger()


@dataclass
class DistributeResult:
    """Result of coupon distribution."""
    success: bool
    coupon_code: Optional[str]
    trial_hours: Optional[int]
    traffic_gb: Optional[int]
    message: str
    batch_key: Optional[str] = None


@dataclass
class CouponBatchResult:
    """Result of group coupon batch generation."""

    success: bool
    coupon_codes: list[str]
    message: str
    batch_key: Optional[str] = None


@dataclass
class EligibilityResult:
    """Result of eligibility check."""
    eligible: bool
    reason: Optional[str]


class CouponDistributor:
    """
    Distributes coupons and rewards to users.
    
    Handles:
    - Trial distribution
    - Discount coupons
    - Gift cards
    - Reward tracking
    """
    
    def __init__(
        self,
        db: AsyncSession,
        xboard_client=None
    ):
        """
        Initialize CouponDistributor.
        
        Args:
            db: Database session
            xboard_client: Optional XBoard API client
        """
        self.db = db
        self._xboard = xboard_client
        self._lock = asyncio.Lock()
        self.logger = logger.bind(module="coupon_distributor")
    
    def set_xboard_client(self, client) -> None:
        """Set XBoard client."""
        self._xboard = client
    
    async def check_eligibility(
        self,
        user_id: int,
        campaign_id: int,
        batch_key_override: Optional[str] = None,
    ) -> EligibilityResult:
        """
        Check if user is eligible for a campaign.
        
        Args:
            user_id: User ID
            campaign_id: Campaign ID
            
        Returns:
            EligibilityResult
        """
        campaign = await self._get_campaign(campaign_id)
        
        if not campaign:
            return EligibilityResult(eligible=False, reason="Campaign not found")
        
        if not campaign.enabled:
            return EligibilityResult(eligible=False, reason="Campaign is not active")

        reward_policy = self._parse_json_dict(campaign.reward_policy_json)
        batch_key = batch_key_override or self._campaign_batch_key(campaign, reward_policy)
        existing = await self._get_distribution(user_id, campaign_id, batch_key)
        if existing:
            return EligibilityResult(
                eligible=False,
                reason="User has already received this batch reward"
            )
        
        return EligibilityResult(eligible=True, reason=None)
    
    async def distribute_trial(
        self,
        user_id: int,
        campaign_id: int,
        telegram_id: Optional[int] = None
    ) -> DistributeResult:
        """
        Distribute trial to a user.
        
        Args:
            user_id: Internal user ID
            campaign_id: Campaign ID
            telegram_id: Telegram user ID
            
        Returns:
            DistributeResult
        """
        eligibility = await self.check_eligibility(user_id, campaign_id)
        
        if not eligibility.eligible:
            return DistributeResult(
                success=False,
                coupon_code=None,
                trial_hours=None,
                traffic_gb=None,
                message=eligibility.reason or "Not eligible"
            )
        
        campaign = await self._get_campaign(campaign_id)
        reward_policy = self._parse_json_dict(campaign.reward_policy_json) if campaign else {}
        batch_key = self._campaign_batch_key(campaign, reward_policy) if campaign else "default"
        
        coupon_code = None
        
        if self._xboard:
            try:
                coupon_code = await self._create_xboard_trial(
                    user_id=telegram_id or user_id,
                    trial_hours=campaign.trial_hours,
                    traffic_gb=campaign.trial_traffic_gb
                )
            except Exception as e:
                self.logger.error(
                    "xboard_trial_failed",
                    user_id=user_id,
                    error=str(e)
                )
                return DistributeResult(
                    success=False,
                    coupon_code=None,
                    trial_hours=None,
                    traffic_gb=None,
                    message="Failed to create trial"
                )
        
        distribution = CouponDistribution(
            user_id=user_id,
            campaign_id=campaign_id,
            distribution_type="trial",
            coupon_code=coupon_code,
            batch_key=batch_key,
            trial_hours=campaign.trial_hours,
            traffic_gb=campaign.trial_traffic_gb
        )
        
        self.db.add(distribution)
        try:
            await self.db.commit()
            await self.db.refresh(distribution)
        except IntegrityError:
            await self.db.rollback()
            existing = await self._get_distribution(user_id, campaign_id, batch_key)
            return DistributeResult(
                success=False,
                coupon_code=existing.coupon_code if existing else None,
                trial_hours=None,
                traffic_gb=None,
                message="User has already received this batch reward",
                batch_key=batch_key,
            )
        
        self.logger.info(
            "trial_distributed",
            user_id=user_id,
            campaign_id=campaign_id,
            coupon_code=coupon_code
        )
        
        return DistributeResult(
            success=True,
            coupon_code=coupon_code,
            trial_hours=campaign.trial_hours,
            traffic_gb=campaign.trial_traffic_gb,
            message="Trial distributed successfully",
            batch_key=batch_key,
        )
    
    async def distribute_discount(
        self,
        user_id: int,
        campaign_id: int,
        telegram_id: Optional[int] = None,
        batch_key_override: Optional[str] = None,
    ) -> DistributeResult:
        """
        Distribute discount coupon to a user.
        
        Args:
            user_id: Internal user ID
            campaign_id: Campaign ID
            telegram_id: Telegram user ID
            
        Returns:
            DistributeResult
        """
        campaign = await self._get_campaign(campaign_id)
        if not campaign:
            return DistributeResult(
                success=False,
                coupon_code=None,
                trial_hours=None,
                traffic_gb=None,
                message="Campaign not found"
            )

        coupon_code = None
        reward_policy = self._parse_json_dict(campaign.reward_policy_json)
        batch_key = batch_key_override or self._campaign_batch_key(campaign, reward_policy)
        provider = str(reward_policy.get("coupon_provider") or reward_policy.get("provider") or "").lower()

        if provider == "sub2api":
            return await self._distribute_sub2api_discount(
                user_id=user_id,
                telegram_id=telegram_id,
                campaign_id=campaign_id,
                batch_key=batch_key,
            )

        eligibility = await self.check_eligibility(
            user_id,
            campaign_id,
            batch_key_override=batch_key_override,
        )
        if not eligibility.eligible:
            return DistributeResult(
                success=False,
                coupon_code=None,
                trial_hours=None,
                traffic_gb=None,
                message=eligibility.reason or "Not eligible",
                batch_key=batch_key,
            )

        if provider in {"", "xboard"}:
            try:
                coupon_code = await self._create_xboard_discount(
                    user_id=telegram_id or user_id,
                    campaign_name=f"campaign_{campaign_id}"
                )
            except Exception as e:
                self.logger.error(
                    "xboard_discount_failed",
                    user_id=user_id,
                    error=str(e)
                )
        else:
            return DistributeResult(
                success=False,
                coupon_code=None,
                trial_hours=None,
                traffic_gb=None,
                message=f"Unsupported coupon provider: {provider}"
            )
        
        distribution = CouponDistribution(
            user_id=user_id,
            campaign_id=campaign_id,
            distribution_type="discount",
            coupon_code=coupon_code,
            batch_key=batch_key,
        )
        
        self.db.add(distribution)
        await self.db.commit()
        await self.db.refresh(distribution)
        
        self.logger.info(
            "discount_distributed",
            user_id=user_id,
            campaign_id=campaign_id,
            coupon_issued=bool(coupon_code),
        )
        
        return DistributeResult(
            success=True,
            coupon_code=coupon_code,
            trial_hours=None,
            traffic_gb=None,
            message="Discount coupon distributed",
            batch_key=batch_key,
        )

    async def _distribute_sub2api_discount(
        self,
        *,
        user_id: int,
        telegram_id: Optional[int],
        campaign_id: int,
        batch_key: str,
    ) -> DistributeResult:
        """Issue one code while enforcing the campaign batch quota."""
        async with self._lock:
            locked = await self.db.execute(
                select(Campaign).where(Campaign.id == campaign_id).with_for_update()
            )
            campaign = locked.scalar_one_or_none()
            if campaign is None or not campaign.enabled:
                return DistributeResult(
                    success=False,
                    coupon_code=None,
                    trial_hours=None,
                    traffic_gb=None,
                    message="Campaign is not active",
                    batch_key=batch_key,
                )

            reward_policy = self._parse_json_dict(campaign.reward_policy_json)
            existing = await self._get_distribution(user_id, campaign_id, batch_key)
            if existing is not None:
                await self.db.commit()
                return DistributeResult(
                    success=bool(existing.coupon_code),
                    coupon_code=existing.coupon_code,
                    trial_hours=None,
                    traffic_gb=None,
                    message=(
                        "Discount coupon already distributed"
                        if existing.coupon_code
                        else "Coupon distribution is pending"
                    ),
                    batch_key=batch_key,
                )

            quota = self._int_policy(
                reward_policy,
                "coupon_quantity",
                default=1,
                minimum=1,
                maximum=100,
            )
            issued_result = await self.db.execute(
                select(func.count(CouponDistribution.id)).where(
                    CouponDistribution.campaign_id == campaign_id,
                    CouponDistribution.batch_key == batch_key,
                )
            )
            if int(issued_result.scalar_one() or 0) >= quota:
                await self.db.commit()
                return DistributeResult(
                    success=False,
                    coupon_code=None,
                    trial_hours=None,
                    traffic_gb=None,
                    message="Campaign batch coupon quota exhausted",
                    batch_key=batch_key,
                )

            try:
                coupon_code = await self._create_sub2api_discount(
                    user_id=telegram_id or user_id,
                    campaign=campaign,
                    reward_policy=reward_policy,
                    batch_key=batch_key,
                )
            except Exception as exc:
                await self.db.commit()
                self.logger.error(
                    "sub2api_discount_failed",
                    user_id=user_id,
                    campaign_id=campaign_id,
                    error=str(exc),
                )
                return DistributeResult(
                    success=False,
                    coupon_code=None,
                    trial_hours=None,
                    traffic_gb=None,
                    message="Failed to create Sub2API coupon",
                    batch_key=batch_key,
                )

            distribution = CouponDistribution(
                user_id=user_id,
                campaign_id=campaign_id,
                distribution_type="discount",
                coupon_code=coupon_code,
                batch_key=batch_key,
            )
            self.db.add(distribution)
            try:
                await self.db.commit()
            except IntegrityError:
                await self.db.rollback()
                existing = await self._get_distribution(user_id, campaign_id, batch_key)
                if existing is None:
                    raise
                coupon_code = existing.coupon_code

            self.logger.info(
                "discount_distributed",
                user_id=user_id,
                campaign_id=campaign_id,
                coupon_issued=bool(coupon_code),
            )
            return DistributeResult(
                success=bool(coupon_code),
                coupon_code=coupon_code,
                trial_hours=None,
                traffic_gb=None,
                message="Discount coupon distributed",
                batch_key=batch_key,
            )

    async def generate_discount_batch(
        self,
        campaign: Campaign,
        *,
        batch_context: str,
    ) -> CouponBatchResult:
        """Generate a campaign-scoped coupon batch for group broadcasts."""
        reward_policy = self._parse_json_dict(campaign.reward_policy_json)
        provider = str(reward_policy.get("coupon_provider") or reward_policy.get("provider") or "").lower()
        batch_key = self._campaign_batch_key(campaign, reward_policy)

        if provider != "sub2api":
            return CouponBatchResult(
                success=False,
                coupon_codes=[],
                batch_key=batch_key,
                message=f"Unsupported group coupon provider: {provider or 'xboard'}",
            )

        try:
            coupon_codes = await self._create_sub2api_discount_batch(
                campaign=campaign,
                reward_policy=reward_policy,
                batch_context=batch_context,
            )
        except Exception as e:
            self.logger.error(
                "sub2api_group_discount_batch_failed",
                campaign_id=campaign.id,
                batch_key=batch_key,
                batch_context=batch_context,
                error=str(e),
            )
            return CouponBatchResult(
                success=False,
                coupon_codes=[],
                batch_key=batch_key,
                message="Failed to create Sub2API coupon batch",
            )

        self.logger.info(
            "discount_batch_generated",
            campaign_id=campaign.id,
            count=len(coupon_codes),
            batch_key=batch_key,
            batch_context=batch_context,
        )
        return CouponBatchResult(
            success=True,
            coupon_codes=coupon_codes,
            batch_key=batch_key,
            message="Discount coupon batch generated",
        )
    
    async def distribute_gift(
        self,
        user_id: int,
        campaign_id: int,
        gift_card_template_id: int,
        telegram_id: Optional[int] = None
    ) -> DistributeResult:
        """
        Distribute gift card to a user.
        
        Args:
            user_id: Internal user ID
            campaign_id: Campaign ID
            gift_card_template_id: Gift card template ID
            telegram_id: Telegram user ID
            
        Returns:
            DistributeResult
        """
        eligibility = await self.check_eligibility(user_id, campaign_id)
        
        if not eligibility.eligible:
            return DistributeResult(
                success=False,
                coupon_code=None,
                trial_hours=None,
                traffic_gb=None,
                message=eligibility.reason or "Not eligible"
            )
        
        campaign = await self._get_campaign(campaign_id)
        reward_policy = self._parse_json_dict(campaign.reward_policy_json) if campaign else {}
        batch_key = self._campaign_batch_key(campaign, reward_policy) if campaign else "default"
        coupon_code = None
        
        if self._xboard:
            try:
                coupon_code = await self._create_xboard_gift(
                    user_id=telegram_id or user_id,
                    template_id=gift_card_template_id
                )
            except Exception as e:
                self.logger.error(
                    "xboard_gift_failed",
                    user_id=user_id,
                    error=str(e)
                )
        
        distribution = CouponDistribution(
            user_id=user_id,
            campaign_id=campaign_id,
            distribution_type="gift",
            coupon_code=coupon_code,
            batch_key=batch_key,
        )
        
        self.db.add(distribution)
        await self.db.commit()
        await self.db.refresh(distribution)
        
        self.logger.info(
            "gift_distributed",
            user_id=user_id,
            campaign_id=campaign_id,
            coupon_code=coupon_code
        )
        
        return DistributeResult(
            success=True,
            coupon_code=coupon_code,
            trial_hours=None,
            traffic_gb=None,
            message="Gift card distributed",
            batch_key=batch_key,
        )
    
    async def get_user_distributions(
        self,
        user_id: int,
        distribution_type: Optional[str] = None
    ) -> list[CouponDistribution]:
        """
        Get user's distribution history.
        
        Args:
            user_id: User ID
            distribution_type: Optional type filter
            
        Returns:
            List of distributions
        """
        query = select(CouponDistribution).where(
            CouponDistribution.user_id == user_id
        )
        
        if distribution_type:
            query = query.where(
                CouponDistribution.distribution_type == distribution_type
            )
        
        query = query.order_by(CouponDistribution.distributed_at.desc())
        
        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def get_distribution_for_batch(
        self,
        user_id: int,
        campaign_id: int,
        batch_key: str,
    ) -> Optional[CouponDistribution]:
        """Get a user's distribution record for one campaign batch."""
        return await self._get_distribution(user_id, campaign_id, batch_key)
    
    async def _get_campaign(self, campaign_id: int) -> Optional[Campaign]:
        """Get campaign by ID."""
        result = await self.db.execute(
            select(Campaign).where(Campaign.id == campaign_id)
        )
        return result.scalar_one_or_none()
    
    async def _get_distribution(
        self,
        user_id: int,
        campaign_id: int,
        batch_key: str,
    ) -> Optional[CouponDistribution]:
        """Get existing distribution."""
        result = await self.db.execute(
            select(CouponDistribution).where(
                and_(
                    CouponDistribution.user_id == user_id,
                    CouponDistribution.campaign_id == campaign_id,
                    CouponDistribution.batch_key == batch_key,
                )
            )
        )
        return result.scalar_one_or_none()

    def _campaign_batch_key(self, campaign: Campaign, reward_policy: dict[str, Any]) -> str:
        """Resolve the coupon batch key used for one-user-one-batch gating."""
        raw = reward_policy.get("coupon_batch_key") or reward_policy.get("batch_key") or campaign.id
        batch_key = str(raw).strip()
        return batch_key[:100] if batch_key else str(campaign.id)
    
    async def _create_xboard_trial(
        self,
        user_id: int,
        trial_hours: int,
        traffic_gb: int
    ) -> str:
        """Create trial in XBoard."""
        if not self._xboard:
            return f"TRIAL_{user_id}_{datetime.now().strftime('%Y%m%d')}"
        
        return await self._xboard.create_trial(
            user_id=user_id,
            hours=trial_hours,
            traffic_gb=traffic_gb
        )
    
    async def _create_xboard_discount(
        self,
        user_id: int,
        campaign_name: str
    ) -> str:
        """Create discount coupon in XBoard."""
        if not self._xboard:
            return f"DISCOUNT_{user_id}_{datetime.now().strftime('%Y%m%d')}"
        
        return await self._xboard.create_discount(
            user_id=user_id,
            campaign_name=campaign_name
        )
    
    async def _create_xboard_gift(
        self,
        user_id: int,
        template_id: int
    ) -> str:
        """Create gift card in XBoard."""
        if not self._xboard:
            return f"GIFT_{user_id}_{datetime.now().strftime('%Y%m%d')}"
        
        return await self._xboard.create_gift(
            user_id=user_id,
            template_id=template_id
        )

    async def _create_sub2api_discount(
        self,
        user_id: int,
        campaign: Campaign,
        reward_policy: dict[str, Any],
        batch_key: str,
    ) -> str:
        """Create a Sub2API redeem-code coupon."""
        client = self._resolve_sub2api_client(reward_policy)
        code_type, value = self._sub2api_code_value(reward_policy)
        expires_in_days = self._sub2api_expiry_days(campaign)
        idempotency_key = self._sub2api_idempotency_key(
            campaign.id,
            "user",
            batch_key,
            user_id,
        )
        group_id = self._optional_int_policy(reward_policy, "sub2api_group_id")
        validity_days = self._optional_int_policy(reward_policy, "sub2api_validity_days")

        codes = await client.generate_redeem_codes(
            count=1,
            code_type=code_type,
            value=value,
            expires_in_days=expires_in_days,
            group_id=group_id,
            validity_days=validity_days,
            idempotency_key=idempotency_key,
        )

        if not codes:
            raise Sub2APIError("Sub2API did not return any redeem codes")
        return codes[0].code

    async def _create_sub2api_discount_batch(
        self,
        *,
        campaign: Campaign,
        reward_policy: dict[str, Any],
        batch_context: str,
    ) -> list[str]:
        """Create Sub2API redeem-code coupons for a group broadcast batch."""
        client = self._resolve_sub2api_client(reward_policy)
        count = self._int_policy(reward_policy, "coupon_quantity", default=1, minimum=1, maximum=100)
        code_type, value = self._sub2api_code_value(reward_policy)
        expires_in_days = self._sub2api_expiry_days(campaign)
        batch_key = self._campaign_batch_key(campaign, reward_policy)
        normalized_context = str(batch_context or "default").strip()[:120] or "default"
        idempotency_key = self._sub2api_idempotency_key(
            campaign.id,
            "group",
            batch_key,
            normalized_context,
        )
        group_id = self._optional_int_policy(reward_policy, "sub2api_group_id")
        validity_days = self._optional_int_policy(reward_policy, "sub2api_validity_days")

        async with self._lock:
            codes = await client.generate_redeem_codes(
                count=count,
                code_type=code_type,
                value=value,
                expires_in_days=expires_in_days,
                group_id=group_id,
                validity_days=validity_days,
                idempotency_key=idempotency_key,
            )

        coupon_codes = [item.code for item in codes if item.code]
        if not coupon_codes:
            raise Sub2APIError("Sub2API did not return any redeem codes")
        return coupon_codes

    def _sub2api_code_value(self, reward_policy: dict[str, Any]) -> tuple[str, float]:
        code_type = str(
            reward_policy.get("coupon_type")
            or reward_policy.get("redeem_type")
            or "balance"
        ).strip().lower()
        if code_type not in {"balance", "concurrency", "subscription", "invitation"}:
            raise ValueError(f"Unsupported Sub2API coupon type: {code_type}")
        if code_type in {"subscription", "invitation"}:
            return code_type, 0.0

        value = self._float_policy(reward_policy, "coupon_amount", default=None)
        if value is None:
            value = self._float_policy(reward_policy, "amount", default=None)
        if value is None or value <= 0:
            raise ValueError("coupon_amount must be greater than zero for Sub2API coupons")
        return code_type, value

    def _sub2api_expiry_days(self, campaign: Campaign) -> int:
        return max(1, min(math.ceil(int(campaign.validity_hours) / 24), 3650))

    def _sub2api_idempotency_key(self, campaign_id: int, *identity: object) -> str:
        raw = "\x1f".join(str(item) for item in (campaign_id, *identity))
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return f"vanguard-coupon-{campaign_id}-{digest}"

    def _resolve_sub2api_client(self, reward_policy: dict[str, Any]) -> Sub2APIClient:
        base_url = str(reward_policy.get("sub2api_base_url") or settings.SUB2API_BASE_URL or "").strip()
        admin_api_key = str(reward_policy.get("sub2api_admin_api_key") or settings.SUB2API_ADMIN_API_KEY or "").strip()
        timeout = self._float_policy(reward_policy, "sub2api_timeout", default=float(settings.SUB2API_TIMEOUT)) or 5.0

        if not settings.SUB2API_ENABLED and not reward_policy.get("sub2api_enabled"):
            raise ValueError("Sub2API integration is disabled")
        if not base_url:
            raise ValueError("SUB2API_BASE_URL is required")
        if not admin_api_key:
            raise ValueError("SUB2API_ADMIN_API_KEY is required")

        return get_sub2api_client(
            base_url=base_url,
            admin_api_key=admin_api_key,
            instance_name=base_url,
            timeout=timeout,
        )

    def _parse_json_dict(self, raw: Optional[str]) -> dict[str, Any]:
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    def _int_policy(
        self,
        policy: dict[str, Any],
        key: str,
        *,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        try:
            value = int(policy.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(value, maximum))

    def _optional_int_policy(self, policy: dict[str, Any], key: str) -> Optional[int]:
        value = policy.get(key)
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _float_policy(
        self,
        policy: dict[str, Any],
        key: str,
        *,
        default: Optional[float],
    ) -> Optional[float]:
        value = policy.get(key, default)
        if value in (None, ""):
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
