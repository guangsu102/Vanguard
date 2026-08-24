from datetime import datetime

from app.api.proxies import _proxy_to_response
from app.core.account.models import Proxy, ProxyType


def _proxy(*, is_active: bool, consecutive_failures: int) -> Proxy:
    now = datetime.utcnow()
    return Proxy(
        id=1,
        proxy_type=ProxyType.DATACENTER,
        host="127.0.0.1",
        port=8080,
        protocol="http",
        country="US",
        is_active=is_active,
        success_rate=0 if consecutive_failures else 1,
        avg_latency=0,
        consecutive_failures=consecutive_failures,
        created_at=now,
        updated_at=now,
    )


def test_proxy_response_marks_failed_inactive_proxy_as_error():
    response = _proxy_to_response(_proxy(is_active=False, consecutive_failures=3))

    assert response.status == "error"


def test_proxy_response_keeps_manually_inactive_proxy_inactive():
    response = _proxy_to_response(_proxy(is_active=False, consecutive_failures=0))

    assert response.status == "inactive"
