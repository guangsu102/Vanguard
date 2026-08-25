"""Legacy XBoard API client.

This Bearer /bot/... client is kept only for historical bot-matrix reference
code. The active Vanguard mainline uses backend/app/integrations/xboard/client.py
with HMAC signed /api/v1/... requests.
"""
import httpx
from typing import Optional
from loguru import logger


class XBoardAPIClient:
    """XBoard API HTTP 客户端"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: int = 30,
        retry_times: int = 3,
        retry_delay: int = 5
    ):
        logger.warning(
            "legacy_xboard_bot_api_client_initialized; use backend HMAC /api/v1 integration for new Vanguard code"
        )
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.retry_times = retry_times
        self.retry_delay = retry_delay

        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        """异步上下文管理器入口"""
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json"
            },
            timeout=self.timeout
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器退出"""
        if self._client:
            await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        data: Optional[dict] = None,
        params: Optional[dict] = None
    ) -> dict:
        """发送 HTTP 请求"""
        if not self._client:
            await self.__aenter__()

        url = f"{self.base_url}/{path.lstrip('/')}"

        for attempt in range(self.retry_times):
            try:
                response = await self._client.request(
                    method=method,
                    url=url,
                    json=data,
                    params=params
                )

                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 429:
                    # Rate Limit
                    logger.warning(f"API 请求频率限制，等待重试...")
                    import asyncio
                    await asyncio.sleep(self.retry_delay * (attempt + 1))
                    continue
                else:
                    logger.error(f"API 请求失败: {response.status_code} - {response.text}")
                    return {"success": False, "message": response.text}

            except httpx.TimeoutException:
                logger.warning(f"API 请求超时 (尝试 {attempt + 1}/{self.retry_times})")
                if attempt < self.retry_times - 1:
                    import asyncio
                    await asyncio.sleep(self.retry_delay)
                continue
            except Exception as e:
                logger.error(f"API 请求异常: {e}")
                return {"success": False, "message": str(e)}

        return {"success": False, "message": "请求失败，请稍后重试"}

    # ============ 用户相关 API ============

    async def create_trial_user(
        self,
        tg_uid: int,
        username: str,
        validity_hours: int = 24,
        traffic_gb: int = 50
    ) -> dict:
        """创建试用用户"""
        return await self._request(
            method="POST",
            path="/bot/users/create-trial",
            data={
                "tg_uid": tg_uid,
                "username": username,
                "validity_hours": validity_hours,
                "traffic_gb": traffic_gb
            }
        )

    async def bind_telegram(self, tg_uid: int, api_key: str) -> dict:
        """绑定 Telegram 账号"""
        return await self._request(
            method="POST",
            path="/bot/users/bind-tg",
            data={
                "tg_uid": tg_uid,
                "api_key": api_key
            }
        )

    async def get_user_info(self, tg_uid: int) -> dict:
        """获取用户信息"""
        return await self._request(
            method="GET",
            path=f"/bot/users/{tg_uid}/info"
        )

    async def add_traffic(self, tg_uid: int, traffic_mb: int, reason: str = "签到奖励") -> dict:
        """增加用户流量"""
        return await self._request(
            method="POST",
            path=f"/bot/users/{tg_uid}/add-traffic",
            data={
                "traffic_mb": traffic_mb,
                "reason": reason
            }
        )

    # ============ 订阅链接 API ============

    async def get_subscription_link(
        self,
        user_id: int,
        protocol: list = None
    ) -> dict:
        """获取用户订阅链接"""
        if protocol is None:
            protocol = ["clash", "v2ray", "surge"]

        return await self._request(
            method="GET",
            path=f"/bot/users/{user_id}/subscription-link",
            params={"protocol": ",".join(protocol)}
        )

    # ============ 订单相关 API ============

    async def get_unpaid_orders(self, timeout_minutes: int = 30) -> dict:
        """获取超时未付款订单"""
        return await self._request(
            method="GET",
            path="/bot/orders/unpaid",
            params={"timeout_minutes": timeout_minutes}
        )

    async def create_coupon(
        self,
        order_id: str,
        discount_percent: int,
        validity_hours: int
    ) -> dict:
        """创建优惠券"""
        return await self._request(
            method="POST",
            path="/bot/orders/create-coupon",
            data={
                "order_id": order_id,
                "discount_percent": discount_percent,
                "validity_hours": validity_hours
            }
        )

    async def apply_coupon(self, order_id: str, coupon_code: str) -> dict:
        """应用优惠券到订单"""
        return await self._request(
            method="POST",
            path=f"/bot/orders/{order_id}/apply-coupon",
            data={"coupon_code": coupon_code}
        )

    # ============ 节点相关 API ============

    async def get_node_status(self) -> dict:
        """获取节点状态"""
        return await self._request(
            method="GET",
            path="/bot/nodes/status"
        )

    # ============ 推广相关 API ============

    async def get_affiliate_link(self, tg_uid: int) -> dict:
        """获取用户推广链接"""
        return await self._request(
            method="GET",
            path=f"/bot/users/{tg_uid}/affiliate-link"
        )
