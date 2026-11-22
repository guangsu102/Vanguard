"""Offline Redis channel semantics, including atomic lifecycle scripts."""

from __future__ import annotations

import asyncio
import time


class FakeRedis:
    def __init__(self):
        self.lists = {}
        self.strings = {}
        self.ttl = {}
        self.brpop_timeouts = []
        self.hashes = {}

    async def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.strings:
            return False
        self.strings[key] = value
        self.ttl[key] = ex or 0
        return True

    async def get(self, key):
        return self.strings.get(key)

    async def mget(self, keys):
        return [self.strings.get(key) for key in keys]

    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    async def rpop(self, key):
        items = self.lists.get(key)
        return items.pop() if items else None

    async def brpop(self, key, timeout=5):
        self.brpop_timeouts.append(timeout)
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            value = await self.rpop(key)
            if value is not None:
                return key, value
            await asyncio.sleep(0.001)
        return None

    async def expire(self, key, seconds):
        self.ttl[key] = seconds
        return key in self.strings or key in self.lists

    async def delete(self, *keys):
        count = 0
        for key in keys:
            count += int(key in self.strings or key in self.lists)
            self.strings.pop(key, None)
            self.lists.pop(key, None)
            self.ttl.pop(key, None)
        return count

    async def lrem(self, key, count, value):
        items = self.lists.get(key, [])
        removed = 0
        for index in range(len(items) - 1, -1, -1):
            if items[index] == value and (not count or removed < count):
                items.pop(index)
                removed += 1
        return removed

    async def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value
        return 1

    async def aclose(self):
        pass

    async def eval(self, script, numkeys, *values):
        keys, args = values[:numkeys], values[numkeys:]
        if "session_exec_reply_if_active_v1" in script:
            if keys[0] not in self.strings:
                return 0
            await self.lpush(keys[1], args[0])
            await self.expire(keys[1], int(args[1]))
            return 1
        if "session_exec_cleanup_v1" in script:
            await self.delete(keys[0], keys[1])
            return await self.lrem(keys[2], 0, args[0])
        raise AssertionError("Unexpected Redis script")

    def assert_channel_empty(self):
        assert not any(self.lists.values()), self.lists
        assert not any("session-exec" in key for key in self.strings), self.strings
