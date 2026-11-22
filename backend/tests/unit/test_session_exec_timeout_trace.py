"""Bounded diagnostic extraction and unchanged owner channel semantics."""
from __future__ import annotations

import asyncio
import ast
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.account import session_exec
from tests.fakes.session_exec_redis import FakeRedis




def source_module(source, name="app.modules.acquisition.qualification_service"):
    namespace = {"__name__": name, "asyncio": asyncio}
    exec(compile(source, "C:/private/secret-token-session-path.py", "exec"), namespace)
    return namespace


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_failure", [False, True])
async def test_original_await_survives_async_cleanup_and_context_chain(cleanup_failure):
    namespace = source_module('''async def original_await():
    private_local = "private-message-body-phone-token"
    await asyncio.Event().wait()

async def engine():
    try:
        await original_await()
    except BaseException:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        if cleanup_failure:
            raise TimeoutError("private-exception-text")
        raise
    finally:
        await asyncio.sleep(0)
''')
    namespace["cleanup_failure"] = cleanup_failure
    timer = asyncio.timeout(.01)
    try:
        async with timer:
            await namespace["engine"]()
    except TimeoutError as exc:
        assert timer.expired()
        trace = session_exec._timeout_trace(exc)
        if not cleanup_failure:
            assert isinstance(exc.__cause__, asyncio.CancelledError)
    else:
        pytest.fail("owner timeout did not fire")
    assert any(frame["module_code"] == 3 and frame["line"] == 3
               for frame in trace["timeout_app_frames"])
    assert "private" not in json.dumps(trace)
    assert session_exec.current_actor_account() is None


def test_unknown_module_and_exception_text_are_never_serialized():
    class SecretError(Exception):
        def __str__(self):
            raise AssertionError("exception text must not be read")
    namespace = source_module('''def fail():
    private_local = "private-message-body-phone-token"
    raise SecretError("private-exception-text")
''', name="unknown.private.phone.token")
    namespace["SecretError"] = SecretError
    try:
        namespace["fail"]()
    except SecretError as exc:
        trace = session_exec._timeout_trace(exc)
    assert trace == {"timeout_app_frames": [], "timeout_trace_limited": 0}
    assert "private" not in json.dumps(trace)


def test_cyclic_exception_links_terminate_and_remain_bounded():
    first, second = TimeoutError(), asyncio.CancelledError()
    first.__cause__ = second
    first.__context__ = second
    second.__cause__ = first
    second.__context__ = first
    assert session_exec._timeout_trace(first) == {
        "timeout_app_frames": [], "timeout_trace_limited": 0}


def test_exception_chain_and_frame_limits_are_numeric_only():
    namespace = source_module('''def recurse(depth):
    if depth:
        return recurse(depth-1)
    raise TimeoutError("private-message")
''')
    try:
        namespace["recurse"](100)
    except TimeoutError as exc:
        original = exc
    tail = original
    for _ in range(10):
        linked = TimeoutError("private-link")
        tail.__cause__ = linked
        tail = linked
    trace = session_exec._timeout_trace(original)
    assert len(trace["timeout_app_frames"]) == 8
    assert trace["timeout_trace_limited"] == 1
    for frame in trace["timeout_app_frames"]:
        assert set(frame) == {"chain_code", "module_code", "line"}
        assert all(type(value) is int for value in frame.values())
        assert 0 <= frame["chain_code"] < 4
        assert frame["module_code"] == 3
    assert "private" not in json.dumps(trace)


def test_sixty_four_frame_limit_is_shared_by_all_exception_links():
    visits = []

    class Trace:
        tb_lineno = 7

        def __init__(self, next_trace):
            self.tb_next = next_trace

        @property
        def tb_frame(self):
            visits.append(1)
            return SimpleNamespace(f_globals={
                "__name__": "app.modules.acquisition.qualification_engine"})

    def chain():
        trace = None
        for _ in range(40):
            trace = Trace(trace)
        return trace

    error = None
    for _ in range(4):
        error = SimpleNamespace(__traceback__=chain(), __cause__=error, __context__=None)
    trace = session_exec._timeout_trace(error)
    assert len(visits) == 64
    assert len(trace["timeout_app_frames"]) == 8
    assert trace["timeout_trace_limited"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("account_id,stage,canary_trace", [
    (33, "review", True), (34, "review", False), (33, "ads", False),
])
async def test_only_expired_canary_review_adds_existing_timeout_log_fields(
        monkeypatch, account_id, stage, canary_trace):
    client = FakeRedis()
    events = []
    cleaned = asyncio.Event()
    original_trace = session_exec._timeout_trace
    trace_calls = []

    def project(exc):
        trace_calls.append(exc)
        return original_trace(exc)

    monkeypatch.setattr(session_exec, "_timeout_trace", project)
    monkeypatch.setattr(session_exec, "logger", SimpleNamespace(
        info=lambda event, **fields: events.append((event, fields)),
        warning=lambda event, **fields: events.append((event, fields))))
    monkeypatch.setattr(session_exec, "STAGE_TIMEOUT_SECONDS", .02)
    namespace = source_module('''async def stage(account_id, name):
    try:
        await asyncio.Event().wait()
    finally:
        await asyncio.sleep(0)
        cleaned.set()
''')
    namespace["cleaned"] = cleaned
    server = asyncio.create_task(session_exec.serve_requests(
        client, owns=lambda _: True, execute_stage=namespace["stage"], poll_interval=.001))
    request_id = "fixed-test-request"
    await client.lpush(session_exec.REQUEST_KEY, json.dumps({
        "id": request_id, "account_id": account_id, "stage": stage,
        "expires_at": time.time() + 1000}))
    try:
        reply = await client.brpop(session_exec.REPLY_PREFIX + request_id, timeout=1)
        payload = json.loads(reply[1])
        assert payload["result"]["reason"] == "session_owner_stage_timeout"
        assert cleaned.is_set()
    finally:
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)
    logged = [fields for event, fields in events if event == "session_exec_stage_timed_out"]
    assert len(logged) == 1
    assert len(trace_calls) == int(canary_trace)
    assert ("timeout_app_frames" in logged[0]) is canary_trace
    if canary_trace:
        assert any(frame["module_code"] == 3 and frame["line"] == 3
                   for frame in logged[0]["timeout_app_frames"])
    assert session_exec.current_actor_account() is None


@pytest.mark.asyncio
async def test_internal_timeout_does_not_run_canary_projection(monkeypatch):
    client = FakeRedis()
    calls = []
    monkeypatch.setattr(session_exec, "_timeout_trace", lambda exc: calls.append(exc))

    async def stage(account_id, name):
        raise TimeoutError("private-rpc-timeout")

    server = asyncio.create_task(session_exec.serve_requests(
        client, owns=lambda _: True, execute_stage=stage, poll_interval=.001))
    try:
        reply = await session_exec.forward(33, "review", client=client, timeout=1)
        assert reply == {"error": "execution_failed", "error_type": "TimeoutError"}
        assert not calls
    finally:
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)
