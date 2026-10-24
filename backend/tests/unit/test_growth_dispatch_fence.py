import pytest

from app.core.scheduler import growth_dispatch


class FenceRedis:
    def __init__(self):
        self.values = {}

    async def set(self, key, value, *, nx=False, ex=None):
        assert ex == growth_dispatch.ACCOUNT_RUN_TTL
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def eval(self, script, numkeys, key, token):
        assert numkeys == 1
        if self.values.get(key) != token:
            return 0
        if "DEL" in script:
            del self.values[key]
        return 1


class PriorityFenceRedis(FenceRedis):
    def __init__(self):
        super().__init__()
        self.priority = {}

    async def eval(self, script, numkeys, *args):
        if numkeys == 1:
            key, token = args
            if self.values.get(key) != token:
                return 0
            if "DEL" in script:
                del self.values[key]
            return 1
        if "local released" in script:
            run_key, priority_key, token = args
            released = int(self.values.get(run_key) == token)
            if released:
                del self.values[run_key]
            if self.priority.get(priority_key) == token:
                del self.priority[priority_key]
            return released
        run_key, priority_key, token, critical, priority_ttl, run_ttl = args
        if critical == "1":
            if run_key not in self.values or priority_key not in self.priority:
                self.priority[priority_key] = token
        elif priority_key in self.priority:
            return -1
        if run_key in self.values:
            return 0
        self.values[run_key] = token
        return 1


@pytest.mark.asyncio
async def test_account_run_fence_serializes_stages_and_owner_checked_release():
    redis = FenceRedis()

    assert await growth_dispatch.claim_account_run(redis, 2, "review:one")
    assert not await growth_dispatch.claim_account_run(redis, 2, "join:two")
    assert not await growth_dispatch.release_account_run(redis, 2, "join:two")
    assert await growth_dispatch.release_account_run(redis, 2, "review:one")
    assert await growth_dispatch.claim_account_run(redis, 2, "join:two")


def test_account_run_key_is_account_scoped():
    assert growth_dispatch.account_run_key(2) != growth_dispatch.account_run_key(3)


@pytest.mark.asyncio
async def test_protected_stage_marks_account_before_background_retry():
    redis = PriorityFenceRedis()

    assert await growth_dispatch.claim_account_run(redis, 2, "review:one", stage="review")
    # The protected ticket records demand even while the review is still
    # running. A new review must defer instead of taking the next free turn.
    assert not await growth_dispatch.claim_account_run(redis, 2, "join:two", stage="join")
    assert not await growth_dispatch.claim_account_run(redis, 2, "review:three", stage="review")

    assert await growth_dispatch.release_account_run(redis, 2, "review:one", stage="review")
    assert await growth_dispatch.claim_account_run(redis, 2, "join:two", stage="join")
    assert await growth_dispatch.release_account_run(redis, 2, "join:two", stage="join")
    assert not redis.priority
