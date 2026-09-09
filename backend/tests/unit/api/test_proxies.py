from datetime import datetime

import pytest

from app.api.proxies import _proxy_to_response, list_proxies, proxy_health
from app.core.account.models import Proxy, ProxyType


def _proxy(
    *,
    is_active: bool,
    consecutive_failures: int,
    proxy_id: int | None = 1,
    success_rate: float | None = None,
    avg_latency: int = 0,
) -> Proxy:
    now = datetime.utcnow()
    return Proxy(
        id=proxy_id,
        proxy_type=ProxyType.DATACENTER,
        host="127.0.0.1",
        port=8080,
        protocol="http",
        country="US",
        is_active=is_active,
        success_rate=((0 if consecutive_failures else 1) if success_rate is None else success_rate),
        avg_latency=avg_latency,
        consecutive_failures=consecutive_failures,
        created_at=now,
        updated_at=now,
    )


def test_proxy_response_keeps_failed_inactive_proxy_inactive():
    response = _proxy_to_response(_proxy(is_active=False, consecutive_failures=3))

    assert response.status == "inactive"


def test_proxy_response_keeps_manually_inactive_proxy_inactive():
    response = _proxy_to_response(_proxy(is_active=False, consecutive_failures=0))

    assert response.status == "inactive"


def test_proxy_response_marks_failed_active_proxy_as_error():
    response = _proxy_to_response(_proxy(is_active=True, consecutive_failures=3))

    assert response.status == "error"


@pytest.mark.asyncio
async def test_proxy_health_excludes_inactive_and_failed_proxies_from_averages(test_db):
    test_db.add_all(
        [
            _proxy(
                is_active=False,
                consecutive_failures=1365,
                proxy_id=None,
                success_rate=0,
                avg_latency=190,
            ),
            _proxy(
                is_active=True,
                consecutive_failures=0,
                proxy_id=None,
                success_rate=1,
                avg_latency=400,
            ),
            _proxy(
                is_active=True,
                consecutive_failures=3,
                proxy_id=None,
                success_rate=0.2,
                avg_latency=900,
            ),
        ]
    )
    await test_db.commit()

    response = await proxy_health(test_db)

    assert response.data["total"] == 3
    assert response.data["active"] == 1
    assert response.data["inactive"] == 1
    assert response.data["error"] == 1
    assert response.data["avg_success_rate"] == 100
    assert response.data["avg_latency_ms"] == 400


@pytest.mark.asyncio
async def test_proxy_error_filter_excludes_inactive_failed_proxy(test_db):
    test_db.add_all(
        [
            _proxy(
                is_active=False,
                consecutive_failures=1365,
                proxy_id=None,
            ),
            _proxy(
                is_active=True,
                consecutive_failures=3,
                proxy_id=None,
            ),
        ]
    )
    await test_db.commit()

    response = await list_proxies(status="error", db=test_db)

    assert response.data["total"] == 1
    assert response.data["list"][0].status == "error"
