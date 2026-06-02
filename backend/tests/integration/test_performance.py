"""
API Performance Tests

Tests API endpoint performance under various load conditions.
"""

import asyncio
import time
from typing import List, Dict, Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, Response

from app.main import app


class PerformanceMetrics:
    """Collect and report performance metrics."""

    def __init__(self):
        self.latencies: List[float] = []
        self.errors: List[str] = []

    def record(self, latency: float, error: str | None = None):
        self.latencies.append(latency)
        if error:
            self.errors.append(error)

    @property
    def total_requests(self) -> int:
        return len(self.latencies)

    @property
    def error_rate(self) -> float:
        if not self.latencies:
            return 0.0
        return len(self.errors) / len(self.latencies) * 100

    @property
    def avg_latency(self) -> float:
        if not self.latencies:
            return 0.0
        return sum(self.latencies) / len(self.latencies)

    @property
    def p50_latency(self) -> float:
        if not self.latencies:
            return 0.0
        sorted_latencies = sorted(self.latencies)
        idx = int(len(sorted_latencies) * 0.5)
        return sorted_latencies[idx]

    @property
    def p95_latency(self) -> float:
        if not self.latencies:
            return 0.0
        sorted_latencies = sorted(self.latencies)
        idx = int(len(sorted_latencies) * 0.95)
        return sorted_latencies[idx]

    @property
    def p99_latency(self) -> float:
        if not self.latencies:
            return 0.0
        sorted_latencies = sorted(self.latencies)
        idx = int(len(sorted_latencies) * 0.99)
        return sorted_latencies[idx]

    @property
    def max_latency(self) -> float:
        if not self.latencies:
            return 0.0
        return max(self.latencies)

    @property
    def min_latency(self) -> float:
        if not self.latencies:
            return 0.0
        return min(self.latencies)

    @property
    def requests_per_second(self) -> float:
        if not self.latencies:
            return 0.0
        total_time = sum(self.latencies)
        if total_time == 0:
            return 0.0
        return len(self.latencies) / total_time

    def report(self) -> Dict[str, Any]:
        return {
            "total_requests": self.total_requests,
            "errors": len(self.errors),
            "error_rate": f"{self.error_rate:.2f}%",
            "latency": {
                "avg": f"{self.avg_latency * 1000:.2f}ms",
                "min": f"{self.min_latency * 1000:.2f}ms",
                "max": f"{self.max_latency * 1000:.2f}ms",
                "p50": f"{self.p50_latency * 1000:.2f}ms",
                "p95": f"{self.p95_latency * 1000:.2f}ms",
                "p99": f"{self.p99_latency * 1000:.2f}ms",
            },
            "rps": f"{self.requests_per_second:.2f}",
        }


async def measure_request(client: AsyncClient, metrics: PerformanceMetrics, method: str, url: str, **kwargs):
    """Measure and record a single request's performance."""
    start = time.perf_counter()
    try:
        response = await client.request(method, url, **kwargs)
        latency = time.perf_counter() - start
        metrics.record(latency, error=None if response.status_code < 400 else f"HTTP {response.status_code}")
    except Exception as e:
        latency = time.perf_counter() - start
        metrics.record(latency, error=str(e))


@pytest.mark.asyncio
async def test_health_endpoint_performance():
    """Test health check endpoint performance."""
    metrics = PerformanceMetrics()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        tasks = [measure_request(client, metrics, "GET", "/health") for _ in range(100)]
        await asyncio.gather(*tasks)

    report = metrics.report()
    print(f"\nHealth Endpoint Performance:\n{report}")

    assert metrics.error_rate == 0, f"Error rate too high: {metrics.error_rate}%"
    assert metrics.p95_latency < 0.1, f"P95 latency too high: {metrics.p95_latency * 1000:.2f}ms"


@pytest.mark.asyncio
async def test_api_endpoints_performance():
    """Test all major API endpoints performance."""
    endpoints = [
        ("GET", "/api/accounts"),
        ("GET", "/api/proxies"),
        ("GET", "/api/groups"),
        ("GET", "/api/keywords"),
        ("GET", "/api/stats/summary"),
    ]

    metrics = PerformanceMetrics()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=30.0) as client:
        for method, endpoint in endpoints:
            tasks = [measure_request(client, metrics, method, endpoint) for _ in range(50)]
            await asyncio.gather(*tasks)

    report = metrics.report()
    print(f"\nAPI Endpoints Performance:\n{report}")

    assert metrics.error_rate < 50, f"Error rate too high: {metrics.error_rate}%"
    assert metrics.p95_latency < 1.0, f"P95 latency too high: {metrics.p95_latency * 1000:.2f}ms"


@pytest.mark.asyncio
async def test_concurrent_requests_performance():
    """Test performance under concurrent load."""
    concurrent_levels = [10, 50, 100]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=60.0) as client:
        for level in concurrent_levels:
            metrics = PerformanceMetrics()
            tasks = [measure_request(client, metrics, "GET", "/health") for _ in range(level)]
            await asyncio.gather(*tasks)

            report = metrics.report()
            print(f"\nConcurrent Level {level}:\n{report}")

            assert metrics.error_rate == 0, f"Error rate too high at concurrency {level}: {metrics.error_rate}%"


@pytest.mark.asyncio
async def test_sustained_load_performance():
    """Test performance under sustained load over time."""
    duration_seconds = 5
    target_rps = 100

    metrics = PerformanceMetrics()
    start_time = time.time()
    request_count = 0

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=30.0) as client:
        while time.time() - start_time < duration_seconds:
            batch_size = min(10, int(target_rps / 2))
            tasks = [measure_request(client, metrics, "GET", "/health") for _ in range(batch_size)]
            await asyncio.gather(*tasks)
            request_count += batch_size

            await asyncio.sleep(batch_size / target_rps)

    elapsed = time.time() - start_time
    actual_rps = request_count / elapsed

    report = metrics.report()
    report["actual_rps"] = f"{actual_rps:.2f}"
    print(f"\nSustained Load Performance ({elapsed:.2f}s):\n{report}")

    assert metrics.error_rate < 5, f"Error rate too high under sustained load: {metrics.error_rate}%"


@pytest.mark.asyncio
async def test_database_query_performance():
    """Test database query performance through API."""
    metrics = PerformanceMetrics()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=30.0) as client:
        for _ in range(100):
            start = time.perf_counter()
            try:
                response = await client.get("/api/groups")
                latency = time.perf_counter() - start
                metrics.record(latency, error=None if response.status_code < 500 else f"HTTP {response.status_code}")
            except Exception as e:
                latency = time.perf_counter() - start
                metrics.record(latency, error=str(e))

    report = metrics.report()
    print(f"\nDatabase Query Performance:\n{report}")

    assert metrics.p95_latency < 0.5, f"Database query P95 latency too high: {metrics.p95_latency * 1000:.2f}ms"


@pytest.mark.asyncio
async def test_response_time_consistency():
    """Test that response times remain consistent across multiple requests."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first_batch = PerformanceMetrics()
        tasks = [measure_request(client, first_batch, "GET", "/health") for _ in range(50)]
        await asyncio.gather(*tasks)

        await asyncio.sleep(1)

        second_batch = PerformanceMetrics()
        tasks = [measure_request(client, second_batch, "GET", "/health") for _ in range(50)]
        await asyncio.gather(*tasks)

    first_avg = first_batch.avg_latency
    second_avg = second_batch.avg_latency

    variance = abs(second_avg - first_avg) / first_avg * 100 if first_avg > 0 else 0

    print(f"\nResponse Time Consistency:\n"
          f"  First batch avg: {first_avg * 1000:.2f}ms\n"
          f"  Second batch avg: {second_avg * 1000:.2f}ms\n"
          f"  Variance: {variance:.2f}%")

    assert variance < 50, f"Response time variance too high: {variance:.2f}%"


@pytest.mark.asyncio
async def test_memory_under_load():
    """Test memory usage remains stable under load."""
    import psutil
    import os

    process = psutil.Process(os.getpid())
    initial_memory = process.memory_info().rss / 1024 / 1024

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=60.0) as client:
        for _ in range(10):
            tasks = [measure_request(client, PerformanceMetrics(), "GET", "/health") for _ in range(100)]
            await asyncio.gather(*tasks)

    final_memory = process.memory_info().rss / 1024 / 1024
    memory_increase = final_memory - initial_memory
    memory_increase_percent = (memory_increase / initial_memory) * 100 if initial_memory > 0 else 0

    print(f"\nMemory Usage:\n"
          f"  Initial: {initial_memory:.2f} MB\n"
          f"  Final: {final_memory:.2f} MB\n"
          f"  Increase: {memory_increase:.2f} MB ({memory_increase_percent:.2f}%)")

    assert memory_increase_percent < 100, f"Memory increase too high: {memory_increase_percent:.2f}%"
