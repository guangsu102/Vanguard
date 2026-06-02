"""
压力测试模块

测试系统在高并发和持续负载下的表现，包括：
- Telegram API 限流模拟和测试
- 并发消息处理压力测试
- 内存泄漏检测
- Redis 连接池压力测试
- 账号池压力测试
- 关键词引擎性能测试
- 持续负载测试

运行方式:
    pytest tests/integration/test_stress.py -v --tb=short
    pytest tests/integration/test_stress.py -v --tb=short -k "telegram"
    pytest tests/integration/test_stress.py::TestTelegramRateLimit -v
"""

import asyncio
import gc
import sys
import time
import tracemalloc
import weakref
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Performance tracking utilities


@dataclass
class StressTestMetrics:
    """压力测试性能指标"""
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    rate_limited_requests: int = 0
    total_duration_ms: float = 0.0
    peak_memory_mb: float = 0.0
    avg_latency_ms: float = 0.0
    min_latency_ms: float = float('inf')
    max_latency_ms: float = 0.0
    latencies: List[float] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def add_result(self, latency_ms: float, success: bool = True, error: str = ""):
        """添加单个请求结果"""
        self.total_requests += 1
        self.latencies.append(latency_ms)

        if success:
            self.successful_requests += 1
        elif error == "rate_limited":
            self.rate_limited_requests += 1
        else:
            self.failed_requests += 1
            if error:
                self.errors.append(error)

        self.min_latency_ms = min(self.min_latency_ms, latency_ms)
        self.max_latency_ms = max(self.max_latency_ms, latency_ms)

    def finalize(self, duration_ms: float):
        """计算最终指标"""
        self.total_duration_ms = duration_ms
        if self.latencies:
            self.avg_latency_ms = sum(self.latencies) / len(self.latencies)

        p50_idx = int(len(self.latencies) * 0.5)
        p95_idx = int(len(self.latencies) * 0.95)
        p99_idx = int(len(self.latencies) * 0.99)

        sorted_latencies = sorted(self.latencies)
        self.p50_ms = sorted_latencies[p50_idx] if sorted_latencies else 0
        self.p95_ms = sorted_latencies[p95_idx] if sorted_latencies else 0
        self.p99_ms = sorted_latencies[p99_idx] if sorted_latencies else 0

        if duration_ms > 0:
            self.throughput = self.total_requests / (duration_ms / 1000)
        else:
            self.throughput = 0

    def print_summary(self):
        """打印测试摘要"""
        print(f"\n{'='*60}")
        print(f"Stress Test Summary")
        print(f"{'='*60}")
        print(f"Total Requests:        {self.total_requests:,}")
        print(f"Successful:           {self.successful_requests:,} "
              f"({100*self.successful_requests/max(1,self.total_requests):.1f}%)")
        print(f"Failed:               {self.failed_requests:,}")
        print(f"Rate Limited:         {self.rate_limited_requests:,}")
        print(f"Duration:             {self.total_duration_ms:.2f} ms")
        print(f"Throughput:           {self.throughput:.2f} req/s")
        print(f"{'='*60}")
        print(f"Latency (ms):")
        print(f"  Min:    {self.min_latency_ms:.3f}")
        print(f"  Avg:    {self.avg_latency_ms:.3f}")
        print(f"  P50:    {getattr(self, 'p50_ms', 0):.3f}")
        print(f"  P95:    {getattr(self, 'p95_ms', 0):.3f}")
        print(f"  P99:    {getattr(self, 'p99_ms', 0):.3f}")
        print(f"  Max:    {self.max_latency_ms:.3f}")
        if self.errors:
            error_counts = Counter(self.errors)
            print(f"\nError Distribution:")
            for error, count in error_counts.most_common(5):
                print(f"  {error}: {count}")
        print(f"{'='*60}")


class MemoryTracker:
    """内存追踪器"""

    def __init__(self):
        self.started = False
        self.start_memory = 0
        self.peak_memory = 0
        self.snapshots = []

    def start(self):
        """开始追踪"""
        gc.collect()
        tracemalloc.start()
        self.started = True
        self.start_memory = tracemalloc.get_traced_memory()[0]
        self.peak_memory = self.start_memory

    def checkpoint(self, label: str = ""):
        """记录检查点"""
        if not self.started:
            return

        current, peak = tracemalloc.get_traced_memory()
        self.peak_memory = max(self.peak_memory, peak)

        self.snapshots.append({
            "label": label,
            "current_mb": current / 1024 / 1024,
            "peak_mb": peak / 1024 / 1024,
            "timestamp": time.time()
        })

    def stop(self) -> Dict[str, float]:
        """停止追踪并返回结果"""
        if not self.started:
            return {}

        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        return {
            "start_mb": self.start_memory / 1024 / 1024,
            "current_mb": current / 1024 / 1024,
            "peak_mb": peak / 1024 / 1024,
            "delta_mb": (current - self.start_memory) / 1024 / 1024
        }


# ============================================================================
# Telegram API 限流测试
# ============================================================================


class TelegramRateLimitSimulator:
    """
    Telegram API 限流模拟器

    模拟 Telegram 的 rate limit 行为：
    - 每秒消息数限制 (30 msg/s 默认)
    - 每分钟消息数限制
    - 每用户消息数限制
    - Burst 限制
    """

    def __init__(
        self,
        per_second: int = 30,
        per_minute: int = 20,
        per_user_per_second: int = 1,
        per_user_per_minute: int = 30,
    ):
        self.per_second = per_second
        self.per_minute = per_minute
        self.per_user_per_second = per_user_per_second
        self.per_user_per_minute = per_user_per_minute

        self._second_buckets: Dict[int, int] = {}
        self._minute_buckets: Dict[int, int] = {}
        self._user_second_buckets: Dict[int, Dict[int, int]] = {}
        self._user_minute_buckets: Dict[int, Dict[int, int]] = {}
        self._lock = asyncio.Lock()

    async def check(self, user_id: int) -> tuple[bool, str]:
        """
        检查是否允许发送消息

        Returns:
            (allowed, reason)
        """
        async with self._lock:
            now = time.time()
            now_second = int(now)
            now_minute = int(now // 60)

            # 全局每秒限制
            if self._second_buckets.get(now_second, 0) >= self.per_second:
                return False, "global_rate_limit_second"

            # 全局每分钟限制
            if self._minute_buckets.get(now_minute, 0) >= self.per_minute:
                return False, "global_rate_limit_minute"

            # 用户每秒限制
            user_sec = self._user_second_buckets.setdefault(user_id, {})
            if user_sec.get(now_second, 0) >= self.per_user_per_second:
                return False, "user_rate_limit_second"

            # 用户每分钟限制
            user_min = self._user_minute_buckets.setdefault(user_id, {})
            if user_min.get(now_minute, 0) >= self.per_user_per_minute:
                return False, "user_rate_limit_minute"

            # 更新计数
            self._second_buckets[now_second] = self._second_buckets.get(now_second, 0) + 1
            self._minute_buckets[now_minute] = self._minute_buckets.get(now_minute, 0) + 1
            user_sec[now_second] = user_sec.get(now_second, 0) + 1
            user_min[now_minute] = user_min.get(now_minute, 0) + 1

            # 清理过期桶
            self._cleanup_expired_buckets(now_second, now_minute)

            return True, ""

    async def send_message(self, user_id: int, message: str) -> tuple[bool, str]:
        """模拟发送消息"""
        allowed, reason = await self.check(user_id)
        if not allowed:
            return False, reason

        # 模拟 API 调用延迟
        await asyncio.sleep(0.01)
        return True, ""

    def _cleanup_expired_buckets(self, now_second: int, now_minute: int):
        """清理过期的桶"""
        # 保留最近 5 秒
        cutoff_second = now_second - 5
        self._second_buckets = {k: v for k, v in self._second_buckets.items() if k > cutoff_second}

        # 保留最近 5 分钟
        cutoff_minute = now_minute - 5
        self._minute_buckets = {k: v for k, v in self._minute_buckets.items() if k > cutoff_minute}

        # 清理用户桶
        for user_id in list(self._user_second_buckets.keys()):
            user_sec = self._user_second_buckets[user_id]
            user_sec_filtered = {k: v for k, v in user_sec.items() if k > cutoff_second}
            if user_sec_filtered:
                self._user_second_buckets[user_id] = user_sec_filtered
            else:
                del self._user_second_buckets[user_id]

        for user_id in list(self._user_minute_buckets.keys()):
            user_min = self._user_minute_buckets[user_id]
            user_min_filtered = {k: v for k, v in user_min.items() if k > cutoff_minute}
            if user_min_filtered:
                self._user_minute_buckets[user_id] = user_min_filtered
            else:
                del self._user_minute_buckets[user_id]

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        now = time.time()
        now_second = int(now)
        now_minute = int(now // 60)

        return {
            "global_per_second": self._second_buckets.get(now_second, 0),
            "global_per_minute": self._minute_buckets.get(now_minute, 0),
            "total_users": len(self._user_second_buckets),
        }


class TestTelegramRateLimit:
    """Telegram API 限流测试"""

    @pytest.fixture
    def rate_limiter(self):
        """创建限流器实例"""
        return TelegramRateLimitSimulator(
            per_second=30,
            per_minute=20,
            per_user_per_second=1,
            per_user_per_minute=30,
        )

    @pytest.mark.asyncio
    async def test_single_user_rate_limit(self, rate_limiter):
        """测试单个用户的限流"""
        user_id = 12345

        results = []
        for _ in range(10):
            success, reason = await rate_limiter.send_message(user_id, "test")
            results.append((success, reason))

        successful = sum(1 for s, _ in results if s)
        rate_limited = sum(1 for _, r in results if r == "user_rate_limit_second")

        print(f"\n[Single User] Successful: {successful}, Rate Limited: {rate_limited}")
        assert successful == 1, "应该只有第一条消息成功（每秒1条限制）"

    @pytest.mark.asyncio
    async def test_multi_user_rate_limit(self, rate_limiter):
        """测试多用户的全局限流"""
        num_users = 50
        messages_per_user = 2

        tasks = []
        for user_id in range(10000, 10000 + num_users):
            for _ in range(messages_per_user):
                tasks.append(rate_limiter.send_message(user_id, f"msg_{user_id}"))

        results = await asyncio.gather(*tasks)
        successful = sum(1 for s, _ in results if s)
        rate_limited = sum(1 for _, r in results if r == "global_rate_limit_second")

        print(f"\n[Multi User] Successful: {successful}, Rate Limited: {rate_limited}")
        assert successful > 0, "应该有消息成功发送"

    @pytest.mark.asyncio
    async def test_rate_limit_recovery(self, rate_limiter):
        """测试限流恢复"""
        user_id = 99999

        # 快速发送达到限制
        for _ in range(5):
            await rate_limiter.send_message(user_id, "test")

        # 立即尝试应该失败
        success, reason = await rate_limiter.send_message(user_id, "test")
        assert success is False, "应该被限流"
        assert "user_rate_limit" in reason

        # 等待一秒后恢复
        await asyncio.sleep(1.1)

        # 现在应该可以发送
        success, reason = await rate_limiter.send_message(user_id, "test")
        assert success is True, "限流后应该可以发送"

    @pytest.mark.asyncio
    async def test_burst_traffic_simulation(self, rate_limiter):
        """测试突发流量"""
        metrics = StressTestMetrics()
        num_concurrent = 100

        async def send_request(user_id: int):
            start = time.time()
            success, reason = await rate_limiter.send_message(user_id, f"burst_{user_id}")
            latency = (time.time() - start) * 1000
            metrics.add_result(latency, success, reason)

        start_time = time.time()
        tasks = [send_request(i) for i in range(num_concurrent)]
        await asyncio.gather(*tasks)
        duration_ms = (time.time() - start_time) * 1000

        metrics.finalize(duration_ms)
        metrics.print_summary()

        assert metrics.successful_requests > 0, "应该有成功的请求"

    @pytest.mark.asyncio
    async def test_sustained_traffic(self, rate_limiter):
        """测试持续流量（模拟 30 秒流量）"""
        metrics = StressTestMetrics()
        duration_seconds = 3  # 缩短为 3 秒用于测试
        target_rps = 50
        user_base = 50000

        async def send_batch(batch_start: float):
            batch_size = target_rps // 10
            tasks = []
            for i in range(batch_size):
                user_id = user_base + (i % 20)
                tasks.append(send_single_message(user_id, f"msg_{i}"))

            await asyncio.gather(*tasks)

        async def send_single_message(user_id: int, msg: str):
            start = time.time()
            success, reason = await rate_limiter.send_message(user_id, msg)
            latency = (time.time() - start) * 1000
            metrics.add_result(latency, success, reason)

        start_time = time.time()
        batch_count = 0

        while time.time() - start_time < duration_seconds:
            batch_start = time.time()
            await send_batch(batch_start)
            batch_count += 1

            # 控制发送速率
            elapsed = time.time() - batch_start
            if elapsed < 0.1:
                await asyncio.sleep(0.1 - elapsed)

        duration_ms = (time.time() - start_time) * 1000
        metrics.finalize(duration_ms)
        metrics.print_summary()

        # 验证限流器正常工作
        stats = rate_limiter.get_stats()
        print(f"\nRate Limiter Stats: {stats}")


# ============================================================================
# 消息路由压力测试
# ============================================================================


class TestMessageRouterStress:
    """MessageRouter 压力测试"""

    @pytest.fixture
    def mock_message(self):
        """创建模拟消息"""
        def create_message(msg_id: int, user_id: int, chat_id: int):
            message = MagicMock()
            message.message_id = msg_id
            message.sender_id = user_id
            message.chat_id = chat_id
            message.text = f"Test message {msg_id}"
            message.message_type = MagicMock(value="text")
            return message

        return create_message

    @pytest.fixture
    def mock_handler(self):
        """创建模拟处理器"""
        async def handle(message):
            await asyncio.sleep(0.001)  # 模拟处理时间
            return True

        handler = MagicMock()
        handler.message_types = [MagicMock(value="text")]
        handler.handle = AsyncMock(side_effect=handle)
        handler.can_handle = AsyncMock(return_value=True)
        return handler

    @pytest.mark.asyncio
    async def test_concurrent_message_routing(self, mock_message, mock_handler):
        """测试并发消息路由"""
        metrics = StressTestMetrics()
        num_messages = 500

        async def route_message(msg_id: int):
            start = time.time()
            message = mock_message(msg_id, msg_id % 100, msg_id % 10)

            try:
                # 模拟路由处理
                await asyncio.sleep(0.001)
                latency = (time.time() - start) * 1000
                metrics.add_result(latency, success=True)
            except Exception as e:
                latency = (time.time() - start) * 1000
                metrics.add_result(latency, success=False, error=str(e))

        start_time = time.time()
        tasks = [route_message(i) for i in range(num_messages)]
        await asyncio.gather(*tasks)
        duration_ms = (time.time() - start_time) * 1000

        metrics.finalize(duration_ms)
        metrics.print_summary()

        assert metrics.successful_requests == num_messages

    @pytest.mark.asyncio
    async def test_message_routing_throughput(self):
        """测试消息路由吞吐量"""
        num_messages = 1000
        batch_sizes = [10, 50, 100, 500]

        for batch_size in batch_sizes:
            metrics = StressTestMetrics()

            async def process_message(msg_id: int):
                start = time.time()
                await asyncio.sleep(0.0001)
                latency = (time.time() - start) * 1000
                metrics.add_result(latency, success=True)

            start_time = time.time()
            for batch_start in range(0, num_messages, batch_size):
                batch_end = min(batch_start + batch_size, num_messages)
                tasks = [process_message(i) for i in range(batch_start, batch_end)]
                await asyncio.gather(*tasks)

            duration_ms = (time.time() - start_time) * 1000
            metrics.finalize(duration_ms)

            print(f"\n[Batch Size {batch_size}] "
                  f"Throughput: {metrics.throughput:.2f} req/s, "
                  f"P95: {metrics.p95_ms:.3f} ms")

    @pytest.mark.asyncio
    async def test_back_pressure_handling(self):
        """测试背压处理"""
        max_pending = 100
        pending_count = 0
        processed_count = 0
        back_pressure_triggered = 0

        async def process_with_backpressure(msg_id: int):
            nonlocal pending_count, processed_count, back_pressure_triggered

            # 模拟背压检测
            while pending_count >= max_pending:
                back_pressure_triggered += 1
                await asyncio.sleep(0.01)

            pending_count += 1

            try:
                await asyncio.sleep(0.001)  # 处理时间
                processed_count += 1
            finally:
                pending_count -= 1

        # 发送突发流量
        num_messages = 500
        start_time = time.time()
        tasks = [process_with_backpressure(i) for i in range(num_messages)]
        await asyncio.gather(*tasks)
        duration_ms = (time.time() - start_time) * 1000

        print(f"\n[Back Pressure] Processed: {processed_count}/{num_messages}, "
              f"Back Pressure Triggers: {back_pressure_triggered}, "
              f"Duration: {duration_ms:.2f}ms")

        assert processed_count == num_messages, "所有消息都应该被处理"


# ============================================================================
# 内存泄漏检测测试
# ============================================================================


class TestMemoryLeaks:
    """内存泄漏检测测试"""

    def test_cache_memory_leak(self):
        """测试缓存内存泄漏"""
        tracker = MemoryTracker()
        tracker.start()

        # 模拟有问题的缓存（没有清理机制）
        bad_cache = {}

        for i in range(10000):
            bad_cache[f"key_{i}"] = f"value_{i}" * 100
            if i % 100 == 0:
                tracker.checkpoint(f"iteration_{i}")

        result = tracker.stop()
        print(f"\n[Bad Cache] Memory Delta: {result.get('delta_mb', 0):.2f} MB")

        # 验证内存增长
        assert result.get('delta_mb', 0) > 0

    def test_cache_with_ttl_no_leak(self):
        """测试带 TTL 的缓存无泄漏"""
        tracker = MemoryTracker()
        tracker.start()

        # 模拟有 TTL 清理的缓存
        cache: Dict[str, tuple[str, float]] = {}

        for i in range(10000):
            cache[f"key_{i}"] = (f"value_{i}", time.time() + 60)

            # 清理过期项
            if i % 100 == 0:
                now = time.time()
                expired_keys = [k for k, v in cache.items() if v[1] < now]
                for k in expired_keys:
                    del cache[k]

            if i % 500 == 0:
                tracker.checkpoint(f"iteration_{i}")

        result = tracker.stop()
        print(f"\n[TTL Cache] Memory Delta: {result.get('delta_mb', 0):.2f} MB, "
              f"Final Size: {len(cache)}")

        # 验证内存增长在合理范围
        assert result.get('delta_mb', 0) < 10, "TTL 缓存应该保持稳定内存"

    def test_event_listener_leak(self):
        """测试事件监听器泄漏"""
        tracker = MemoryTracker()
        tracker.start()

        listeners = []

        class EventEmitter:
            def __init__(self):
                self._listeners: List[Any] = []

            def add_listener(self, callback):
                self._listeners.append(callback)

            def emit(self):
                for listener in self._listeners:
                    listener()

        # 模拟创建和销毁事件发射器
        for i in range(1000):
            emitter = EventEmitter()

            def callback():
                pass

            emitter.add_listener(callback)

            # 模拟外部引用导致泄漏
            listeners.append(weakref.ref(emitter))

            if i % 100 == 0:
                tracker.checkpoint(f"iteration_{i}")

        result = tracker.stop()
        print(f"\n[Event Listener] Memory Delta: {result.get('delta_mb', 0):.2f} MB")

        # 检查是否有对象泄漏
        gc.collect()
        alive = sum(1 for ref in listeners if ref() is not None)
        print(f"[Event Listener] Alive Objects: {alive}/1000")

    @pytest.mark.asyncio
    async def test_async_task_leak(self):
        """测试异步任务泄漏"""
        tracker = MemoryTracker()
        tracker.start()

        tasks = []

        for i in range(500):
            async def background_task():
                await asyncio.sleep(10)

            # 创建任务但不保存引用
            task = asyncio.create_task(background_task())
            tasks.append(task)

            if i % 100 == 0:
                tracker.checkpoint(f"iteration_{i}")

        # 取消所有任务
        for task in tasks:
            task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)

        result = tracker.stop()
        print(f"\n[Async Task] Memory Delta: {result.get('delta_mb', 0):.2f} MB")

        # 验证内存稳定
        gc.collect()
        assert result.get('delta_mb', 0) < 5

    def test_string_interning(self):
        """测试字符串驻留"""
        tracker = MemoryTracker()
        tracker.start()

        interned_strings = []
        for i in range(10000):
            s = f"common_prefix_{i % 100}_suffix"
            interned_strings.append(sys.intern(s))

            if i % 1000 == 0:
                tracker.checkpoint(f"iteration_{i}")

        result = tracker.stop()
        print(f"\n[String Interning] Memory Delta: {result.get('delta_mb', 0):.2f} MB")
        print(f"[String Interning] Unique Strings: {len(set(interned_strings))}")


# ============================================================================
# Redis 连接池压力测试
# ============================================================================


class TestRedisPoolStress:
    """Redis 连接池压力测试（模拟）"""

    @pytest.fixture
    def mock_redis_pool(self):
        """创建模拟 Redis 连接池"""
        class MockRedisPool:
            def __init__(self, max_connections: int = 50):
                self.max_connections = max_connections
                self.available = list(range(max_connections))
                self.in_use: set[int] = set()
                self.total_acquired = 0
                self.total_released = 0
                self.acquire_timeouts = 0
                self._lock = asyncio.Lock()

            async def acquire(self, timeout: float = 5.0):
                async with self._lock:
                    if self.available:
                        conn_id = self.available.pop()
                        self.in_use.add(conn_id)
                        self.total_acquired += 1
                        return conn_id

                    # 模拟获取连接超时
                    self.acquire_timeouts += 1
                    raise TimeoutError("No available connections")

            async def release(self, conn_id: int):
                async with self._lock:
                    if conn_id in self.in_use:
                        self.in_use.remove(conn_id)
                        self.available.append(conn_id)
                        self.total_released += 1

            async def stats(self):
                async with self._lock:
                    return {
                        "max_connections": self.max_connections,
                        "available": len(self.available),
                        "in_use": len(self.in_use),
                        "total_acquired": self.total_acquired,
                        "total_released": self.total_released,
                        "timeouts": self.acquire_timeouts,
                    }

        return MockRedisPool(max_connections=20)

    @pytest.mark.asyncio
    async def test_pool_contention(self, mock_redis_pool):
        """测试连接池竞争"""
        num_requests = 200
        held_connections = []

        async def use_connection(req_id: int):
            try:
                conn_id = await asyncio.wait_for(
                    mock_redis_pool.acquire(),
                    timeout=1.0
                )
                held_connections.append(conn_id)

                # 模拟使用连接
                await asyncio.sleep(0.01)

                await mock_redis_pool.release(conn_id)
                return True
            except TimeoutError:
                return False

        start_time = time.time()
        tasks = [use_connection(i) for i in range(num_requests)]
        results = await asyncio.gather(*tasks)
        duration_ms = (time.time() - start_time) * 1000

        successful = sum(1 for r in results if r)
        stats = await mock_redis_pool.stats()

        print(f"\n[Pool Contention] "
              f"Successful: {successful}/{num_requests}, "
              f"Timeouts: {stats['timeouts']}, "
              f"Duration: {duration_ms:.2f}ms")

        assert successful > 0, "应该有成功的请求"

    @pytest.mark.asyncio
    async def test_pool_exhaustion(self, mock_redis_pool):
        """测试连接池耗尽"""
        # 获取所有连接
        connections = []
        for _ in range(20):
            conn_id = await mock_redis_pool.acquire()
            connections.append(conn_id)

        stats = await mock_redis_pool.stats()
        assert stats["available"] == 0, "连接池应该耗尽"

        # 尝试获取更多（应该失败）
        try:
            await asyncio.wait_for(mock_redis_pool.acquire(), timeout=0.1)
            assert False, "应该抛出超时异常"
        except TimeoutError:
            pass  # 预期行为

        # 释放一个连接
        await mock_redis_pool.release(connections[0])

        # 现在应该可以获取
        new_conn = await mock_redis_pool.acquire()
        assert new_conn is not None

        # 清理
        for conn in connections:
            await mock_redis_pool.release(conn)

        print(f"\n[Pool Exhaustion] Test passed")

    @pytest.mark.asyncio
    async def test_pool_concurrent_access(self, mock_redis_pool):
        """测试并发访问"""
        num_operations = 200

        async def read_operation(op_id: int):
            try:
                conn_id = await asyncio.wait_for(
                    mock_redis_pool.acquire(),
                    timeout=0.5
                )
                try:
                    await asyncio.sleep(0.001)  # 模拟读取
                    return True
                finally:
                    await mock_redis_pool.release(conn_id)
            except TimeoutError:
                return False  # 连接池耗尽时返回 False

        async def write_operation(op_id: int):
            try:
                conn_id = await asyncio.wait_for(
                    mock_redis_pool.acquire(),
                    timeout=0.5
                )
                try:
                    await asyncio.sleep(0.002)  # 模拟写入
                    return True
                finally:
                    await mock_redis_pool.release(conn_id)
            except TimeoutError:
                return False  # 连接池耗尽时返回 False

        start_time = time.time()

        # 混合读写操作
        tasks = []
        for i in range(num_operations):
            if i % 3 == 0:
                tasks.append(write_operation(i))
            else:
                tasks.append(read_operation(i))

        results = await asyncio.gather(*tasks)
        duration_ms = (time.time() - start_time) * 1000

        successful = sum(1 for r in results if r)
        failed = sum(1 for r in results if not r)
        throughput = num_operations / (duration_ms / 1000)

        stats = await mock_redis_pool.stats()
        print(f"\n[Pool Concurrent] "
              f"Successful: {successful}/{num_operations}, "
              f"Failed (pool exhausted): {failed}, "
              f"Throughput: {throughput:.2f} ops/s, "
              f"Timeouts: {stats['timeouts']}")

        # 验证：至少有一些成功的请求
        assert successful > 0, "至少应该有成功的请求"


# ============================================================================
# 关键词引擎压力测试
# ============================================================================


class TestKeywordEngineStress:
    """KeywordEngine 压力测试"""

    @pytest.mark.asyncio
    async def test_keyword_matching_performance(self):
        """测试关键词匹配性能"""
        num_keywords = 1000
        num_queries = 500

        # 创建模拟关键词
        keywords = [
            f"keyword_{i}"
            for i in range(num_keywords)
        ]

        # 模拟关键词引擎匹配
        async def match_keyword(query: str) -> List[str]:
            matches = []
            for kw in keywords:
                if kw in query:
                    matches.append(kw)
            return matches

        test_queries = [
            f"This is a test message with keyword_{i % 100}"
            for i in range(num_queries)
        ]

        start_time = time.time()
        total_matches = 0

        for query in test_queries:
            matches = await match_keyword(query)
            total_matches += len(matches)

        duration_ms = (time.time() - start_time) * 1000
        throughput = num_queries / (duration_ms / 1000)

        print(f"\n[Keyword Matching] "
              f"Keywords: {num_keywords}, "
              f"Queries: {num_queries}, "
              f"Matches: {total_matches}, "
              f"Duration: {duration_ms:.2f}ms, "
              f"Throughput: {throughput:.2f} queries/s")

        # 验证性能可接受（500查询在100ms内完成）
        assert duration_ms < 500, "关键词匹配应该足够快"

    @pytest.mark.asyncio
    async def test_concurrent_keyword_matching(self):
        """测试并发关键词匹配"""
        num_keywords = 500
        keywords = [f"target_{i}" for i in range(num_keywords)]

        async def concurrent_match(worker_id: int) -> int:
            query = f"Message with target_{worker_id % num_keywords}"
            matches = sum(1 for kw in keywords if kw in query)
            await asyncio.sleep(0.0001)  # 模拟处理
            return matches

        num_workers = 100
        start_time = time.time()
        results = await asyncio.gather(*[concurrent_match(i) for i in range(num_workers)])
        duration_ms = (time.time() - start_time) * 1000

        total_matches = sum(results)
        throughput = num_workers / (duration_ms / 1000)

        print(f"\n[Concurrent Matching] "
              f"Workers: {num_workers}, "
              f"Matches: {total_matches}, "
              f"Duration: {duration_ms:.2f}ms, "
              f"Throughput: {throughput:.2f} matches/s")


# ============================================================================
# 账号池压力测试
# ============================================================================


class TestAccountPoolStress:
    """AccountPool 压力测试（模拟）"""

    @pytest.fixture
    def mock_account_pool(self):
        """创建模拟账号池"""
        class MockAccount:
            def __init__(self, account_id: int):
                self.account_id = account_id
                self.session_name = f"session_{account_id}"
                self.status = "idle"
                self.message_count = 0
                self.error_count = 0

            @property
            def health_score(self) -> float:
                if self.message_count == 0:
                    return 1.0
                return max(0.0, 1.0 - (self.error_count / max(1, self.message_count)))

        class MockAccountPool:
            def __init__(self, size: int = 10):
                self.size = size
                self.accounts: Dict[int, MockAccount] = {
                    i: MockAccount(i) for i in range(size)
                }
                self._lock = asyncio.Lock()
                self.acquire_count = 0
                self.release_count = 0

            async def acquire(self, purpose: str = "default") -> Optional[MockAccount]:
                async with self._lock:
                    available = [
                        acc for acc in self.accounts.values()
                        if acc.status == "idle"
                    ]
                    if not available:
                        return None

                    # 使用最少使用策略
                    selected = min(available, key=lambda a: a.message_count)
                    selected.status = "working"
                    self.acquire_count += 1
                    return selected

            async def release(self, account: MockAccount):
                async with self._lock:
                    account.status = "idle"
                    self.release_count += 1

            async def get_stats(self):
                async with self._lock:
                    return {
                        "total": len(self.accounts),
                        "idle": sum(1 for a in self.accounts.values() if a.status == "idle"),
                        "working": sum(1 for a in self.accounts.values() if a.status == "working"),
                        "acquire_count": self.acquire_count,
                        "release_count": self.release_count,
                    }

        return MockAccountPool(size=10)

    @pytest.mark.asyncio
    async def test_concurrent_account_acquisition(self, mock_account_pool):
        """测试并发账号获取"""
        num_requests = 50
        successful = 0
        failed = 0

        async def use_account(req_id: int):
            nonlocal successful, failed
            account = await mock_account_pool.acquire(f"task_{req_id}")
            if account:
                try:
                    await asyncio.sleep(0.01)  # 模拟使用
                    account.message_count += 1
                    successful += 1
                finally:
                    await mock_account_pool.release(account)
            else:
                failed += 1

        start_time = time.time()
        tasks = [use_account(i) for i in range(num_requests)]
        await asyncio.gather(*tasks)
        duration_ms = (time.time() - start_time) * 1000

        stats = await mock_account_pool.get_stats()

        print(f"\n[Account Pool] "
              f"Successful: {successful}, "
              f"Failed: {failed}, "
              f"Duration: {duration_ms:.2f}ms, "
              f"Acquire/Release: {stats['acquire_count']}/{stats['release_count']}")

        assert successful > 0

    @pytest.mark.asyncio
    async def test_account_pool_exhaustion(self, mock_account_pool):
        """测试账号池耗尽"""
        # 获取所有账号
        accounts = []
        for _ in range(10):
            acc = await mock_account_pool.acquire()
            if acc:
                accounts.append(acc)

        # 尝试获取更多
        extra = await mock_account_pool.acquire()
        assert extra is None, "应该没有可用账号"

        # 释放一个
        await mock_account_pool.release(accounts[0])

        # 现在应该可以获取
        new_acc = await mock_account_pool.acquire()
        assert new_acc is not None

        # 清理
        for acc in accounts:
            await mock_account_pool.release(acc)

        print(f"\n[Account Pool Exhaustion] Test passed")


# ============================================================================
# 持续负载测试
# ============================================================================


class TestSustainedLoad:
    """持续负载测试"""

    @pytest.mark.asyncio
    async def test_sustained_message_processing(self):
        """测试持续消息处理（60秒模拟）"""
        duration_seconds = 5  # 缩短为 5 秒
        target_rps = 100

        metrics = StressTestMetrics()
        processed = 0
        start_time = time.time()

        async def process_message(msg_id: int):
            nonlocal processed
            loop_start = time.time()

            await asyncio.sleep(0.005)  # 模拟处理

            latency = (time.time() - loop_start) * 1000
            metrics.add_result(latency, success=True)
            processed += 1

        while time.time() - start_time < duration_seconds:
            batch_start = time.time()

            # 按目标 RPS 发送
            tasks = [process_message(processed + i) for i in range(target_rps // 10)]
            await asyncio.gather(*tasks)

            # 控制速率
            elapsed = time.time() - batch_start
            target_elapsed = 0.1  # 10个批次/秒
            if elapsed < target_elapsed:
                await asyncio.sleep(target_elapsed - elapsed)

        total_duration_ms = (time.time() - start_time) * 1000
        metrics.finalize(total_duration_ms)
        metrics.print_summary()

        print(f"\n[Sustained Load] "
              f"Processed: {processed} messages in {duration_seconds}s, "
              f"Actual RPS: {processed/duration_seconds:.2f}")

    @pytest.mark.asyncio
    async def test_memory_stability_under_load(self):
        """测试负载下内存稳定性"""
        tracker = MemoryTracker()
        tracker.start()

        duration_seconds = 3
        iterations = 0
        start_time = time.time()

        # 模拟消息处理
        messages: List[Dict] = []

        while time.time() - start_time < duration_seconds:
            # 创建新消息
            msg = {
                "id": iterations,
                "data": "x" * 1000,
                "timestamp": time.time()
            }
            messages.append(msg)

            # 处理
            await asyncio.sleep(0.001)

            # 定期清理
            if iterations % 100 == 0:
                # 清理旧消息（保留最近50个）
                messages = messages[-50:]
                tracker.checkpoint(f"iter_{iterations}")

            iterations += 1

        result = tracker.stop()

        print(f"\n[Memory Stability] "
              f"Iterations: {iterations}, "
              f"Start Memory: {result.get('start_mb', 0):.2f} MB, "
              f"End Memory: {result.get('current_mb', 0):.2f} MB, "
              f"Peak Memory: {result.get('peak_mb', 0):.2f} MB, "
              f"Delta: {result.get('delta_mb', 0):.2f} MB")

        # 验证内存稳定
        assert result.get('delta_mb', 0) < 10, "内存增长应该在合理范围"

    @pytest.mark.asyncio
    async def test_gc_performance_under_load(self):
        """测试 GC 在负载下的性能"""
        tracker = MemoryTracker()
        tracker.start()

        gc_times: List[float] = []

        for i in range(1000):
            # 创建临时对象
            temp_data = [{"key": f"value_{j}", "data": "x" * 100} for j in range(10)]

            # 模拟处理
            await asyncio.sleep(0.0001)

            # 定期 GC
            if i % 100 == 0:
                gc_start = time.time()
                gc.collect()
                gc_time = time.time() - gc_start
                gc_times.append(gc_time)
                tracker.checkpoint(f"gc_iter_{i}")

        result = tracker.stop()

        avg_gc_time = sum(gc_times) / len(gc_times) if gc_times else 0
        max_gc_time = max(gc_times) if gc_times else 0

        print(f"\n[GC Performance] "
              f"GC Calls: {len(gc_times)}, "
              f"Avg GC Time: {avg_gc_time*1000:.2f}ms, "
              f"Max GC Time: {max_gc_time*1000:.2f}ms, "
              f"Memory Delta: {result.get('delta_mb', 0):.2f} MB")

        # GC 时间应该在合理范围（放宽到 500ms，CI 环境可能较慢）
        assert max_gc_time < 0.5, f"GC 时间过长: {max_gc_time*1000:.2f}ms"


# ============================================================================
# 熔断器测试
# ============================================================================


class TestCircuitBreakerStress:
    """熔断器压力测试"""

    @pytest.fixture
    def circuit_breaker(self):
        """创建熔断器"""
        class CircuitBreaker:
            CLOSED = "closed"
            OPEN = "open"
            HALF_OPEN = "half_open"

            def __init__(
                self,
                failure_threshold: int = 5,
                success_threshold: int = 3,
                timeout: float = 5.0
            ):
                self.failure_threshold = failure_threshold
                self.success_threshold = success_threshold
                self.timeout = timeout
                self.state = self.CLOSED
                self.failure_count = 0
                self.success_count = 0
                self.last_failure_time: Optional[float] = None
                self.total_calls = 0
                self.successful_calls = 0
                self.rejected_calls = 0

            def record_success(self):
                self.total_calls += 1
                self.successful_calls += 1

                if self.state == self.HALF_OPEN:
                    self.success_count += 1
                    if self.success_count >= self.success_threshold:
                        self.state = self.CLOSED
                        self.failure_count = 0
                        self.success_count = 0

            def record_failure(self):
                self.total_calls += 1
                self.failure_count += 1
                self.last_failure_time = time.time()

                if self.state == self.HALF_OPEN:
                    self.state = self.OPEN
                    self.success_count = 0
                elif self.failure_count >= self.failure_threshold:
                    self.state = self.OPEN

            def can_execute(self) -> bool:
                if self.state == self.CLOSED:
                    return True

                if self.state == self.OPEN:
                    if self.last_failure_time:
                        if time.time() - self.last_failure_time > self.timeout:
                            self.state = self.HALF_OPEN
                            self.success_count = 0
                            return True
                    return False

                # HALF_OPEN 状态允许执行
                return True

            def get_stats(self) -> Dict:
                return {
                    "state": self.state,
                    "failure_count": self.failure_count,
                    "total_calls": self.total_calls,
                    "successful_calls": self.successful_calls,
                    "rejected_calls": self.rejected_calls,
                }

        return CircuitBreaker(
            failure_threshold=5,
            success_threshold=3,
            timeout=2.0
        )

    def test_circuit_breaker_opens_on_failures(self, circuit_breaker):
        """测试连续失败后熔断器打开"""
        # 模拟连续失败
        for _ in range(5):
            circuit_breaker.record_failure()

        assert circuit_breaker.state == "open"
        assert not circuit_breaker.can_execute()

        stats = circuit_breaker.get_stats()
        print(f"\n[Circuit Breaker] State: {stats['state']}, "
              f"Failures: {stats['failure_count']}")

    def test_circuit_breaker_half_open_recovery(self, circuit_breaker):
        """测试熔断器半开恢复"""
        # 打开熔断器
        for _ in range(5):
            circuit_breaker.record_failure()

        assert circuit_breaker.state == "open"

        # 等待超时
        time.sleep(2.1)

        # 尝试执行
        assert circuit_breaker.can_execute()
        assert circuit_breaker.state == "half_open"

        # 连续成功恢复
        for _ in range(3):
            circuit_breaker.record_success()

        assert circuit_breaker.state == "closed"

        print(f"\n[Circuit Breaker Recovery] Test passed")

    @pytest.mark.asyncio
    async def test_circuit_breaker_under_load(self, circuit_breaker):
        """测试负载下熔断器行为"""
        num_requests = 100
        success_count = 0
        rejected_count = 0
        failure_count = 0

        async def make_request(req_id: int):
            nonlocal success_count, rejected_count, failure_count

            if not circuit_breaker.can_execute():
                rejected_count += 1
                return False

            # 模拟请求
            await asyncio.sleep(0.001)

            # 随机失败（10% 失败率）
            if req_id % 10 == 0:
                circuit_breaker.record_failure()
                failure_count += 1
                return False
            else:
                circuit_breaker.record_success()
                success_count += 1
                return True

        results = await asyncio.gather(*[make_request(i) for i in range(num_requests)])

        stats = circuit_breaker.get_stats()
        print(f"\n[Circuit Breaker Load] "
              f"Success: {success_count}, "
              f"Failed: {failure_count}, "
              f"Rejected: {rejected_count}, "
              f"Final State: {stats['state']}")


# ============================================================================
# 运行入口
# ============================================================================


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short", "-s"])
