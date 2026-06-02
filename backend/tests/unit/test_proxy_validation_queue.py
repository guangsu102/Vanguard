import importlib

import pytest

from app.core.account.models import Proxy, ProxyType


proxies_api = importlib.import_module("app.api.proxies")


class DummyResult:
    id = "proxy-validation-task-123"


@pytest.mark.asyncio
async def test_batch_validate_proxies_is_queued(test_db, monkeypatch):
    proxy = Proxy(
        proxy_type=ProxyType.DATACENTER,
        host="127.0.0.1",
        port=1080,
        protocol="socks5",
        country="US",
        is_active=True,
    )
    test_db.add(proxy)
    await test_db.commit()
    await test_db.refresh(proxy)

    calls = {}

    def fake_apply_async(*args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return DummyResult()

    monkeypatch.setattr(proxies_api.validate_proxy_batch, "apply_async", fake_apply_async)

    response = await proxies_api.batch_validate_proxies(proxy_ids=[proxy.id], db=test_db)

    assert calls["kwargs"] == {"args": [[proxy.id]], "queue": "proxy_validation"}
    assert response["data"]["queued"] is True
    assert response["data"]["task_name"] == "validate_proxy_batch"
    assert response["data"]["task_id"] == "proxy-validation-task-123"
