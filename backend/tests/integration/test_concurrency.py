"""
并发压力测试
测试系统在高并发情况下的表现
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest


class TestConcurrentRequests:
    """并发请求测试"""

    @pytest.fixture
    def mock_api_client(self):
        """模拟 API 客户端"""
        client = MagicMock()
        client.request = AsyncMock(return_value={"code": 0, "data": {}})
        return client

    @pytest.mark.asyncio
    async def test_concurrent_api_calls(self, mock_api_client):
        """测试并发 API 调用"""
        num_requests = 100

        async def make_request(i):
            await mock_api_client.request(f"/api/resource/{i}")
            return i

        start_time = time.perf_counter()
        results = await asyncio.gather(*[make_request(i) for i in range(num_requests)])
        elapsed = time.perf_counter() - start_time

        assert len(results) == num_requests
        print(f"\n[Concurrent API] {num_requests} requests in {elapsed:.2f}s")
        throughput = num_requests / elapsed if elapsed > 0 else float("inf")
        print(f"[Concurrent API] Throughput: {throughput:.2f} req/s")

    @pytest.mark.asyncio
    async def test_concurrent_user_registrations(self, mock_api_client):
        """测试并发用户注册"""
        num_users = 50

        async def register_user(user_id):
            await asyncio.sleep(0.01)  # 模拟网络延迟
            return {"user_id": user_id, "status": "registered"}

        start_time = time.time()
        results = await asyncio.gather(*[register_user(i) for i in range(num_users)])
        elapsed = time.time() - start_time

        assert len(results) == num_users
        assert all(r["status"] == "registered" for r in results)
        print(f"\n[Concurrent Registration] {num_users} registrations in {elapsed:.2f}s")

    @pytest.mark.asyncio
    async def test_concurrent_message_sending(self):
        """测试并发发送消息"""
        num_messages = 200

        async def send_message(msg_id):
            # 模拟发送延迟
            await asyncio.sleep(0.005)
            return {"message_id": msg_id, "sent": True}

        start_time = time.time()
        results = await asyncio.gather(*[send_message(i) for i in range(num_messages)])
        elapsed = time.time() - start_time

        assert len(results) == num_messages
        print(f"\n[Concurrent Messages] {num_messages} messages in {elapsed:.2f}s")


class TestRateLimiting:
    """限流测试"""

    @pytest.fixture
    def rate_limiter(self):
        """简单的内存限流器"""
        class RateLimiter:
            def __init__(self, max_requests: int, window_seconds: int):
                self.max_requests = max_requests
                self.window_seconds = window_seconds
                self.requests = []

            def is_allowed(self) -> bool:
                now = time.time()
                self.requests = [r for r in self.requests if now - r < self.window_seconds]

                if len(self.requests) < self.max_requests:
                    self.requests.append(now)
                    return True
                return False

            def get_remaining(self) -> int:
                now = time.time()
                self.requests = [r for r in self.requests if now - r < self.window_seconds]
                return self.max_requests - len(self.requests)

        return RateLimiter(max_requests=10, window_seconds=1)

    def test_rate_limit_allows_requests(self, rate_limiter):
        """测试限流允许请求"""
        allowed = 0
        for _ in range(10):
            if rate_limiter.is_allowed():
                allowed += 1

        assert allowed == 10

    def test_rate_limit_blocks_excess(self, rate_limiter):
        """测试限流阻止超额请求"""
        # 消耗所有配额
        for _ in range(10):
            rate_limiter.is_allowed()

        # 下一个应该被阻止
        assert rate_limiter.is_allowed() is False

    def test_rate_limit_window_reset(self, rate_limiter):
        """测试限流窗口重置"""
        # 消耗配额
        for _ in range(10):
            rate_limiter.is_allowed()

        assert rate_limiter.is_allowed() is False

        # 等待窗口过期
        time.sleep(1.1)

        # 应该重新允许
        assert rate_limiter.is_allowed() is True


class TestConnectionPooling:
    """连接池测试"""

    @pytest.fixture
    def connection_pool(self):
        """模拟连接池"""
        class ConnectionPool:
            def __init__(self, max_connections: int):
                self.max_connections = max_connections
                self.available = list(range(max_connections))
                self.in_use = set()
                self.lock = asyncio.Lock()

            async def acquire(self):
                async with self.lock:
                    if self.available:
                        conn_id = self.available.pop()
                        self.in_use.add(conn_id)
                        return conn_id
                    raise Exception("No available connections")

            async def release(self, conn_id):
                async with self.lock:
                    if conn_id in self.in_use:
                        self.in_use.remove(conn_id)
                        self.available.append(conn_id)

        return ConnectionPool(max_connections=5)

    @pytest.mark.asyncio
    async def test_pool_acquire_release(self, connection_pool):
        """测试连接池获取和释放"""
        conn_id = await connection_pool.acquire()
        assert conn_id in connection_pool.in_use

        await connection_pool.release(conn_id)
        assert conn_id in connection_pool.available

    @pytest.mark.asyncio
    async def test_pool_exhaustion(self, connection_pool):
        """测试连接池耗尽"""
        # 获取所有连接
        connections = []
        for _ in range(5):
            conn = await connection_pool.acquire()
            connections.append(conn)

        # 尝试获取更多应该失败
        with pytest.raises(Exception, match="No available connections"):
            await connection_pool.acquire()

        # 释放一个
        await connection_pool.release(connections[0])

        # 现在可以获取
        new_conn = await connection_pool.acquire()
        assert new_conn is not None


class TestMemoryLeaks:
    """内存泄漏检测测试"""

    @pytest.mark.asyncio
    async def test_no_memory_leak_in_cache(self):
        """测试缓存无内存泄漏"""
        cache = {}

        # 模拟频繁的缓存操作
        for i in range(10000):
            cache[f"key_{i}"] = f"value_{i}"

            # 模拟 TTL 过期
            if i % 100 == 0:
                # 清理旧条目
                keys_to_delete = [k for k in cache.keys() if int(k.split('_')[1]) < i - 100]
                for k in keys_to_delete:
                    del cache[k]

        # 验证缓存大小合理
        assert len(cache) < 1000  # 应该有有效的清理机制

    @pytest.mark.asyncio
    async def test_no_reference_leak(self):
        """测试无引用泄漏"""
        import gc

        class TestObject:
            def __init__(self, value):
                self.value = value

        objects = []
        for i in range(100):
            obj = TestObject(i)
            objects.append(obj)

        # 删除引用
        objects.clear()
        del obj

        # 强制垃圾回收
        gc.collect()

        # 检查是否有对象残留
        reachable = gc.get_objects()
        test_objects = [o for o in reachable if isinstance(o, TestObject)]
        assert len(test_objects) == 0


class TestBackPressure:
    """背压处理测试"""

    @pytest.fixture
    def queue_with_backpressure(self):
        """带背压的队列"""
        class BackPressureQueue:
            def __init__(self, max_size: int):
                self.max_size = max_size
                self.queue = []
                self.waiters = 0

            async def put(self, item):
                while len(self.queue) >= self.max_size:
                    self.waiters += 1
                    await asyncio.sleep(0.01)
                    self.waiters -= 1

                self.queue.append(item)
                return True

            async def get(self):
                while len(self.queue) == 0:
                    await asyncio.sleep(0.01)
                return self.queue.pop(0)

            def qsize(self):
                return len(self.queue)

        return BackPressureQueue(max_size=100)

    @pytest.mark.asyncio
    async def test_queue_backpressure(self, queue_with_backpressure):
        """测试队列背压"""
        # 填充队列到最大值
        for i in range(100):
            await queue_with_backpressure.put({"id": i})

        assert queue_with_backpressure.qsize() == 100

        # 消费一些
        for _ in range(50):
            await queue_with_backpressure.get()

        assert queue_with_backpressure.qsize() == 50


class TestCircuitBreaker:
    """熔断器测试"""

    @pytest.fixture
    def circuit_breaker(self):
        """模拟熔断器"""
        class CircuitBreaker:
            def __init__(self, failure_threshold: int, timeout: float):
                self.failure_threshold = failure_threshold
                self.timeout = timeout
                self.failure_count = 0
                self.last_failure_time = None
                self.state = "closed"  # closed, open, half_open

            def call(self, func):
                if self.state == "open":
                    if time.time() - self.last_failure_time > self.timeout:
                        self.state = "half_open"
                    else:
                        raise RuntimeError("Circuit breaker is OPEN")

                try:
                    result = func()
                    self.on_success()
                    return result
                except Exception as e:
                    self.on_failure()
                    raise e

            def on_success(self):
                self.failure_count = 0
                if self.state == "half_open":
                    self.state = "closed"

            def on_failure(self):
                self.failure_count += 1
                self.last_failure_time = time.time()
                if self.failure_count >= self.failure_threshold:
                    self.state = "open"

        return CircuitBreaker(failure_threshold=3, timeout=5)

    def test_circuit_breaker_opens(self, circuit_breaker):
        """测试熔断器打开"""
        for _ in range(3):
            with pytest.raises(RuntimeError):
                circuit_breaker.call(lambda: (_ for _ in ()).throw(RuntimeError("Error")))

        assert circuit_breaker.state == "open"

        # 再次调用应该立即失败
        with pytest.raises(RuntimeError, match="Circuit breaker is OPEN"):
            circuit_breaker.call(lambda: True)

    def test_circuit_breaker_half_open(self, circuit_breaker):
        """测试熔断器半开"""
        # 打开熔断器
        for _ in range(3):
            with pytest.raises(RuntimeError):
                circuit_breaker.call(lambda: (_ for _ in ()).throw(RuntimeError()))

        assert circuit_breaker.state == "open"

        # 模拟超时后尝试
        circuit_breaker.last_failure_time = time.time() - 10

        # 成功的半开探测应关闭熔断器并恢复服务
        assert circuit_breaker.call(lambda: True) is True
        assert circuit_breaker.state == "closed"
        assert circuit_breaker.failure_count == 0


class TestPerformanceBenchmarks:
    """性能基准测试"""

    @pytest.mark.asyncio
    async def test_keyword_matching_performance(self):
        """测试关键词匹配性能"""
        keywords = [f"keyword_{i}" for i in range(1000)]
        test_text = "This is a test with keyword_500 in it"

        start_time = time.time()

        # 按完整 token 匹配，避免 keyword_5 命中 keyword_500。
        tokens = set(test_text.split())
        matches = [kw for kw in keywords if kw in tokens]

        elapsed = time.time() - start_time

        assert len(matches) == 1
        print(f"\n[Keyword Matching] 1000 keywords in {elapsed*1000:.2f}ms")

    @pytest.mark.asyncio
    async def test_json_serialization_performance(self):
        """测试 JSON 序列化性能"""
        import json

        data = {
            "users": [
                {"id": i, "name": f"user_{i}", "email": f"user{i}@example.com"}
                for i in range(100)
            ]
        }

        start_time = time.time()

        for _ in range(1000):
            json.dumps(data)
            json.loads(json.dumps(data))

        elapsed = time.time() - start_time
        print(f"\n[JSON Serialization] 1000 serialize/deserialize in {elapsed:.2f}s")


class TestStressTesting:
    """压力测试"""

    @pytest.mark.asyncio
    async def test_sustained_load(self):
        """测试持续负载"""
        duration = 2  # 秒
        request_count = 0

        async def handle_request():
            nonlocal request_count
            await asyncio.sleep(0.01)  # 模拟处理
            request_count += 1

        start_time = time.time()
        tasks = []

        while time.time() - start_time < duration:
            tasks.append(asyncio.create_task(handle_request()))

            # 控制并发
            if len(tasks) >= 50:
                await asyncio.gather(*tasks, return_exceptions=True)
                tasks = []

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        elapsed = time.time() - start_time
        throughput = request_count / elapsed

        print(f"\n[Sustained Load] {request_count} requests in {elapsed:.2f}s")
        print(f"[Sustained Load] Throughput: {throughput:.2f} req/s")

    @pytest.mark.asyncio
    async def test_burst_traffic(self):
        """测试突发流量"""
        burst_size = 500

        async def burst_request(i):
            await asyncio.sleep(0.001)
            return i

        start_time = time.time()
        results = await asyncio.gather(*[burst_request(i) for i in range(burst_size)])
        elapsed = time.time() - start_time

        assert len(results) == burst_size
        print(f"\n[Burst Traffic] {burst_size} concurrent requests in {elapsed:.2f}s")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
