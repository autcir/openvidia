"""Replay timestamps identify when events occurred, not dashboard receipt time."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from openvidia import proxy_state, webui
from openvidia.proxy_state import ProxyState, ProxyStats


def state_with_logs(callback=lambda _msg: None):
    return ProxyState([], ProxyStats(), callback)


@pytest.mark.asyncio
async def test_history_and_live_events_keep_original_times(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(proxy_state.time, "time", lambda: now[0])
    state = state_with_logs()
    state.log_cb("old event")
    now[0] = 200.0
    state.log_cb("later event")
    app = FastAPI()
    webui.attach_webui(app, state, Path(webui.__file__).parent / "web")
    endpoint = next(route.endpoint for route in app.routes if route.path == "/api/logs/stream")

    async def connected():
        return False

    response = await endpoint(SimpleNamespace(is_disconnected=connected))
    stream = response.body_iterator
    first = json.loads((await anext(stream)).removeprefix("data: ").strip())
    second = json.loads((await anext(stream)).removeprefix("data: ").strip())
    assert first == {"id": 1, "msg": "old event", "timestamp": 100.0}
    assert second == {"id": 2, "msg": "later event", "timestamp": 200.0}
    now[0] = 900.0
    state.log_cb("live event")
    live = json.loads((await anext(stream)).removeprefix("data: ").strip())
    assert live == {"id": 3, "msg": "live event", "timestamp": 900.0}
    await stream.aclose()
    assert state.listeners == set()
    assert list(state.log_buffer) == ["old event", "later event", "live event"]


@pytest.mark.asyncio
async def test_subscribe_before_snapshot_dedupes_overlap_and_loses_no_live_event(monkeypatch):
    state = state_with_logs()
    state.log_cb("initial")

    def subscribe_with_racing_event(queue):
        # Simulate an event occurring after registration but before snapshot.
        state.listeners.add(queue)
        state.log_cb("overlap")
        return list(state.log_events)

    monkeypatch.setattr(state, "subscribe_logs", subscribe_with_racing_event)
    app = FastAPI()
    webui.attach_webui(app, state, Path(webui.__file__).parent / "web")
    endpoint = next(route.endpoint for route in app.routes if route.path == "/api/logs/stream")

    async def connected():
        return False

    stream = (await endpoint(SimpleNamespace(is_disconnected=connected))).body_iterator
    events = [json.loads((await anext(stream)).removeprefix("data: ").strip()) for _ in range(2)]
    state.log_cb("after snapshot")
    events.append(json.loads((await anext(stream)).removeprefix("data: ").strip()))
    assert [event["msg"] for event in events] == ["initial", "overlap", "after snapshot"]
    assert [event["id"] for event in events] == [1, 2, 3]
    await stream.aclose()
    assert not state.listeners


@pytest.mark.asyncio
async def test_log_credentials_are_redacted_from_all_outputs():
    printed = []
    state = state_with_logs(printed.append)
    queue = asyncio.Queue()
    state.subscribe_logs(queue)
    state.log_cb(
        "nvapi-private-secret Bearer private-access-token http://localhost/?token=private-control"
    )
    await asyncio.sleep(0)
    event = queue.get_nowait()
    serialized = json.dumps([printed, list(state.log_buffer), list(state.log_events), event])
    for secret in ("nvapi-private-secret", "private-access-token", "private-control"):
        assert secret not in serialized
    assert "REDACTED" in serialized


@pytest.mark.asyncio
async def test_history_and_live_backlog_are_bounded():
    state = state_with_logs()
    queue = asyncio.Queue(maxsize=2)
    state.subscribe_logs(queue)
    for index in range(501):
        state.log_cb(f"event {index}")
    await asyncio.sleep(0)
    assert len(state.log_events) == 500
    assert len(state.log_buffer) == 500
    assert queue.qsize() == 2
    assert queue.get_nowait()["msg"] == "event 499"
    assert queue.get_nowait()["msg"] == "event 500"


@pytest.mark.asyncio
async def test_plain_503_log_says_service_unavailable_not_gateway_timeout():
    import httpx

    from openvidia.responses_shim import _rotation_phase

    state = ProxyState(["first-key", "second-key"], ProxyStats(), lambda _msg: None)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(503, text="unavailable"))
    ) as client:
        await _rotation_phase(
            client,
            "https://upstream.example/v1/chat/completions",
            {"model": "test/model"},
            lambda key, _index: {"Authorization": f"Bearer {key}"},
            state,
            state.get_candidate_keys(),
            max_attempts=1,
            timeout=httpx.Timeout(1),
            stream=False,
            log_tag="probe",
        )
    event = next(
        event for event in state.log_events if "HTTP 503 service unavailable" in event["msg"]
    )
    assert "service unavailable" in event["msg"]
    assert "gateway timeout" not in event["msg"]
