"""
Concurrent Processing Performance Tests

Tests system performance under concurrent load conditions.
"""

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List

import psutil
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.database import get_db
from app.core.security import get_current_user
from app.main import app


@pytest_asyncio.fixture(autouse=True)
async def override_api_dependencies(test_db):
    """Keep protected endpoint load tests authenticated and database-isolated."""
    async def override_get_db():
        yield test_db

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1,
        "username": "performance-test",
        "role": "admin",
    }
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)


@dataclass
class ConcurrencyMetrics:
    """Metrics for concurrent processing tests."""

    concurrent_level: int
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    total_time: float = 0
    latencies: List[float] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        if self.total_requests == 0:
            return 0
        return self.successful_requests / self.total_requests * 100

    @property
    def throughput(self) -> float:
        if self.total_time == 0:
            return 0
        return self.total_requests / self.total_time

    @property
    def avg_latency(self) -> float:
        if not self.latencies:
            return 0
        return sum(self.latencies) / len(self.latencies)

    @property
    def p95_latency(self) -> float:
        if not self.latencies:
            return 0
        sorted_latencies = sorted(self.latencies)
        idx = int(len(sorted_latencies) * 0.95)
        return sorted_latencies[idx]

    def report(self) -> Dict[str, Any]:
        return {
            "concurrent_level": self.concurrent_level,
            "total_requests": self.total_requests,
            "successful_requests": self.successful_requests,
            "failed_requests": self.failed_requests,
            "success_rate": f"{self.success_rate:.2f}%",
            "total_time": f"{self.total_time:.2f}s",
            "throughput": f"{self.throughput:.2f} req/s",
            "latency": {
                "avg": f"{self.avg_latency * 1000:.2f}ms",
                "p95": f"{self.p95_latency * 1000:.2f}ms",
            },
        }


async def concurrent_request_task(
    client: AsyncClient,
    metrics: ConcurrencyMetrics,
    endpoint: str,
    method: str = "GET",
) -> None:
    """Execute a single request and record metrics."""
    start = time.perf_counter()
    try:
        response = await client.request(method, endpoint)
        latency = time.perf_counter() - start

        metrics.total_requests += 1
        metrics.latencies.append(latency)

        if response.status_code < 400:
            metrics.successful_requests += 1
        else:
            metrics.failed_requests += 1
    except Exception:
        latency = time.perf_counter() - start
        metrics.total_requests += 1
        metrics.failed_requests += 1
        metrics.latencies.append(latency)


@pytest.mark.asyncio
async def test_basic_concurrency():
    """Test basic concurrent request handling."""
    levels = [10, 25, 50]

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        timeout=60.0,
    ) as client:
        for level in levels:
            metrics = ConcurrencyMetrics(concurrent_level=level)
            tasks = [
                concurrent_request_task(client, metrics, "/health")
                for _ in range(level)
            ]

            start_time = time.perf_counter()
            await asyncio.gather(*tasks)
            metrics.total_time = time.perf_counter() - start_time

            report = metrics.report()
            print(f"\nConcurrency Level {level}:\n{report}")

            assert metrics.success_rate >= 95, f"Success rate too low at concurrency {level}"


@pytest.mark.asyncio
async def test_mixed_endpoint_concurrency():
    """Test concurrent requests to different endpoints."""
    metrics = ConcurrencyMetrics(concurrent_level=50)

    endpoints = [
        "/health",
        "/api/accounts",
        "/api/proxies",
        "/api/groups",
    ]

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        timeout=60.0,
    ) as client:
        tasks = []
        for _ in range(50):
            for endpoint in endpoints:
                tasks.append(
                    concurrent_request_task(client, metrics, endpoint)
                )

        start_time = time.perf_counter()
        await asyncio.gather(*tasks)
        metrics.total_time = time.perf_counter() - start_time

        report = metrics.report()
        print(f"\nMixed Endpoint Concurrency:\n{report}")

        assert metrics.success_rate >= 90, "Success rate too low for mixed endpoints"


@pytest.mark.asyncio
async def test_ramp_up_concurrency():
    """Test performance as concurrency ramps up gradually."""
    results: List[ConcurrencyMetrics] = []

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        timeout=60.0,
    ) as client:
        for level in [1, 5, 10, 20, 30, 40, 50]:
            metrics = ConcurrencyMetrics(concurrent_level=level)
            tasks = [
                concurrent_request_task(client, metrics, "/health")
                for _ in range(level * 10)
            ]

            start_time = time.perf_counter()
            await asyncio.gather(*tasks)
            metrics.total_time = time.perf_counter() - start_time

            results.append(metrics)

    print("\nRamp-up Concurrency Test:")
    for metrics in results:
        print(f"  Level {metrics.concurrent_level}: "
              f"throughput={metrics.throughput:.2f} req/s, "
              f"success={metrics.success_rate:.2f}%, "
              f"p95={metrics.p95_latency * 1000:.2f}ms")

    baseline = results[0].throughput
    for metrics in results[1:]:
        degradation = (baseline - metrics.throughput) / baseline * 100 if baseline > 0 else 0
        assert degradation < 50, f"Throughput degradation too high at level {metrics.concurrent_level}"


@pytest.mark.asyncio
async def test_sustained_concurrency():
    """Test sustained concurrent load over time."""
    duration_seconds = 3
    concurrent_users = 20

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        timeout=60.0,
    ) as client:
        metrics = ConcurrencyMetrics(concurrent_level=concurrent_users)

        start_time = time.perf_counter()
        while time.perf_counter() - start_time < duration_seconds:
            tasks = [
                concurrent_request_task(client, metrics, "/health")
                for _ in range(concurrent_users)
            ]
            await asyncio.gather(*tasks)

        metrics.total_time = time.perf_counter() - start_time

        report = metrics.report()
        print(f"\nSustained Concurrency ({duration_seconds}s):\n{report}")

        assert metrics.success_rate >= 95, "Success rate too low for sustained load"
        assert metrics.throughput >= 50, f"Throughput too low: {metrics.throughput:.2f}"


@pytest.mark.asyncio
async def test_burst_traffic():
    """Test system response to burst traffic spikes."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        timeout=60.0,
    ) as client:
        metrics_normal = ConcurrencyMetrics(concurrent_level=10)
        tasks = [
            concurrent_request_task(client, metrics_normal, "/health")
            for _ in range(50)
        ]
        start_time = time.perf_counter()
        await asyncio.gather(*tasks)
        metrics_normal.total_time = time.perf_counter() - start_time

        await asyncio.sleep(1)

        metrics_burst = ConcurrencyMetrics(concurrent_level=100)
        tasks = [
            concurrent_request_task(client, metrics_burst, "/health")
            for _ in range(500)
        ]
        start_time = time.perf_counter()
        await asyncio.gather(*tasks)
        metrics_burst.total_time = time.perf_counter() - start_time

        print(f"\nNormal Load:\n{metrics_normal.report()}")
        print(f"\nBurst Traffic:\n{metrics_burst.report()}")

        assert metrics_burst.success_rate >= 80, f"Burst success rate too low: {metrics_burst.success_rate}%"


@pytest.mark.asyncio
async def test_connection_reuse():
    """Test HTTP connection reuse efficiency."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        timeout=60.0,
    ) as client:
        metrics_new = ConcurrencyMetrics(concurrent_level=1)
        for _ in range(100):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as fresh_client:
                await concurrent_request_task(fresh_client, metrics_new, "/health")
        metrics_new.total_time = metrics_new.avg_latency * 100

        metrics_reused = ConcurrencyMetrics(concurrent_level=1)
        for _ in range(100):
            await concurrent_request_task(client, metrics_reused, "/health")
        metrics_reused.total_time = metrics_reused.avg_latency * 100

        print(f"\nConnection Reuse:\n"
              f"  New connections avg: {metrics_new.avg_latency * 1000:.2f}ms\n"
              f"  Reused connections avg: {metrics_reused.avg_latency * 1000:.2f}ms")

        improvement = (metrics_new.avg_latency - metrics_reused.avg_latency) / metrics_new.avg_latency * 100
        print(f"  Improvement: {improvement:.2f}%")


@pytest.mark.asyncio
async def test_cpu_memory_under_load():
    """Test CPU and memory usage under concurrent load."""
    process = psutil.Process(os.getpid())

    initial_memory = process.memory_info().rss / 1024 / 1024
    initial_cpu = process.cpu_percent(interval=0.1)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        timeout=120.0,
    ) as client:
        for _ in range(5):
            tasks = [
                concurrent_request_task(client, ConcurrencyMetrics(concurrent_level=50), "/health")
                for _ in range(50)
            ]
            await asyncio.gather(*tasks)

        await asyncio.sleep(1)

    final_memory = process.memory_info().rss / 1024 / 1024
    final_cpu = process.cpu_percent(interval=0.1)

    memory_increase = final_memory - initial_memory
    memory_increase_pct = (memory_increase / initial_memory) * 100 if initial_memory > 0 else 0

    print(f"\nResource Usage Under Load:\n"
          f"  Memory: {initial_memory:.2f} MB -> {final_memory:.2f} MB "
          f"(+{memory_increase:.2f} MB, {memory_increase_pct:.2f}%)\n"
          f"  CPU: {initial_cpu:.1f}% -> {final_cpu:.1f}%")

    assert memory_increase_pct < 100, f"Memory increase too high: {memory_increase_pct:.2f}%"


@pytest.mark.asyncio
async def test_async_task_scheduling():
    """Test async task scheduling efficiency."""
    async def cpu_bound_task(task_id: int) -> float:
        start = time.perf_counter()
        await asyncio.sleep(0.01)
        sum(i * i for i in range(1000))
        return time.perf_counter() - start

    task_counts = [10, 50, 100, 200]

    print("\nAsync Task Scheduling:")
    for count in task_counts:
        start = time.perf_counter()
        tasks = [cpu_bound_task(i) for i in range(count)]
        await asyncio.gather(*tasks)
        total_time = time.perf_counter() - start

        throughput = count / total_time
        print(f"  {count} tasks: {total_time:.2f}s, {throughput:.2f} tasks/s")

        expected_time = count * 0.01
        efficiency = expected_time / total_time * 100
        assert efficiency > 50, f"Task scheduling efficiency too low: {efficiency:.2f}%"


@pytest.mark.asyncio
async def test_backpressure_handling():
    """Test system handling of backpressure."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        timeout=120.0,
    ) as client:
        metrics = ConcurrencyMetrics(concurrent_level=200)
        tasks = [
            concurrent_request_task(client, metrics, "/health")
            for _ in range(200)
        ]

        start_time = time.perf_counter()
        await asyncio.gather(*tasks, return_exceptions=True)
        metrics.total_time = time.perf_counter() - start_time

        report = metrics.report()
        print(f"\nBackpressure Test:\n{report}")

        assert metrics.success_rate >= 70, f"Backpressure handling too poor: {metrics.success_rate}%"


@pytest.mark.asyncio
async def test_throughput_scaling():
    """Test how throughput scales with concurrency."""
    data_points = []

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        timeout=120.0,
    ) as client:
        for concurrent in [1, 5, 10, 20]:
            metrics = ConcurrencyMetrics(concurrent_level=concurrent)
            tasks = [
                concurrent_request_task(client, metrics, "/health")
                for _ in range(concurrent * 20)
            ]

            start_time = time.perf_counter()
            await asyncio.gather(*tasks)
            metrics.total_time = time.perf_counter() - start_time

            data_points.append({
                "concurrent": concurrent,
                "throughput": metrics.throughput,
                "avg_latency": metrics.avg_latency,
            })

    print("\nThroughput Scaling:")
    for point in data_points:
        print(f"  {point['concurrent']} concurrent: "
              f"{point['throughput']:.2f} req/s, "
              f"latency={point['avg_latency'] * 1000:.2f}ms")

    scaling_factor = data_points[-1]["throughput"] / data_points[0]["throughput"]
    max_expected_scaling = data_points[-1]["concurrent"] / data_points[0]["concurrent"]

    print(f"\nScaling efficiency: {scaling_factor:.2f}x (max: {max_expected_scaling:.2f}x)")
