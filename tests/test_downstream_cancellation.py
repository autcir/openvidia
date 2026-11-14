"""A departing loopback probe must not leave inference running for 90 seconds."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from openvidia import proxy_app
from openvidia.proxy_state import ProxyState, ProxyStats


def state():
    return ProxyState(["test-key"], ProxyStats(), lambda _line: None)


@pytest.mark.asyncio
async def test_disconnect_cancels_real_pending_rotation_and_releases_claim():
    model_state = state()
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def upstream(request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def receive():
        await started.wait()
        return {"type": "http.disconnect"}

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        async with asyncio.timeout(1):
            with pytest.raises(proxy_app._DownstreamDisconnected):
                await proxy_app._rotation_until_disconnect(
                    SimpleNamespace(receive=receive),
                    model_state,
                    client,
                    "https://upstream.example/v1/chat/completions",
                    {"model": "test/model"},
                    lambda key, _index: {"Authorization": f"Bearer {key}"},
                    model_state,
                    model_state.get_candidate_keys(),
                    max_attempts=1,
                    timeout=httpx.Timeout(90),
                    stream=True,
                    log_tag="probe",
                )
    assert cancelled.is_set()
    assert model_state.key_states["test-key"].in_flight == 0
    assert not model_state.is_key_on_cooldown("test-key")


@pytest.mark.asyncio
async def test_simultaneous_success_and_disconnect_closes_response_and_own_claim(monkeypatch):
    model_state = state()
    model_state.begin_in_flight("test-key")  # another request owns this claim
    response = httpx.Response(200, content=b"answer")

    async def rotation(*args, **kwargs):
        model_state.begin_in_flight("test-key")
        return response, "test-key", 0

    async def receive():
        return {"type": "http.disconnect"}

    monkeypatch.setattr(proxy_app, "_rotation_phase", rotation)
    with pytest.raises(proxy_app._DownstreamDisconnected):
        await proxy_app._rotation_until_disconnect(SimpleNamespace(receive=receive), model_state)
    assert response.is_closed
    assert model_state.key_states["test-key"].in_flight == 1


@pytest.mark.asyncio
async def test_success_transfers_response_to_caller_and_stops_disconnect_watcher(monkeypatch):
    model_state = state()
    watcher_cancelled = asyncio.Event()
    response = httpx.Response(200, stream=httpx.ByteStream(b"answer"))

    async def rotation(*args, **kwargs):
        model_state.begin_in_flight("test-key")
        return response, "test-key", 0

    async def receive():
        try:
            await asyncio.Event().wait()
        finally:
            watcher_cancelled.set()

    monkeypatch.setattr(proxy_app, "_rotation_phase", rotation)
    result = await proxy_app._rotation_until_disconnect(
        SimpleNamespace(receive=receive), model_state
    )
    assert result == (response, "test-key", 0)
    assert not response.is_closed
    assert model_state.key_states["test-key"].in_flight == 1
    assert watcher_cancelled.is_set()
    await response.aclose()
    model_state.end_in_flight("test-key")


@pytest.mark.asyncio
async def test_parent_cancellation_stops_operation_and_watcher(monkeypatch):
    model_state = state()
    began, stopped = asyncio.Event(), asyncio.Event()

    async def rotation(*args, **kwargs):
        model_state.begin_in_flight("test-key")
        began.set()
        try:
            await asyncio.Event().wait()
        finally:
            model_state.end_in_flight("test-key")
            stopped.set()

    async def receive():
        await asyncio.Event().wait()

    monkeypatch.setattr(proxy_app, "_rotation_phase", rotation)
    task = asyncio.create_task(
        proxy_app._rotation_until_disconnect(SimpleNamespace(receive=receive), model_state)
    )
    await began.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    assert model_state.key_states["test-key"].in_flight == 0


@pytest.mark.asyncio
async def test_cancel_during_watcher_cleanup_does_not_transfer_or_leak_response(monkeypatch):
    model_state = state()
    watcher_closing = asyncio.Event()
    response = httpx.Response(200, stream=httpx.ByteStream(b"answer"))

    async def rotation(*args, **kwargs):
        model_state.begin_in_flight("test-key")
        return response, "test-key", 0

    async def receive():
        try:
            await asyncio.Event().wait()
        finally:
            watcher_closing.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(proxy_app, "_rotation_phase", rotation)
    task = asyncio.create_task(
        proxy_app._rotation_until_disconnect(SimpleNamespace(receive=receive), model_state)
    )
    await watcher_closing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert response.is_closed
    assert model_state.key_states["test-key"].in_flight == 0
