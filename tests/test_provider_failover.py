"""Exercise opt-in provider failover through the actual ASGI chat route."""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager

import httpx
import pytest

from openvidia import config, proxy_app
from openvidia.provider_fallback import ProviderConfig, try_fallback
from openvidia.proxy_state import ProxyState, ProxyStats

MODEL = "vendor/primary"
PROVIDER = ProviderConfig(
    "backup", "https://backup.example/v1", "backup-key", {MODEL: "backup-model"}
)
PAYLOAD = {"model": MODEL, "messages": [{"role": "user", "content": "Hello"}]}


class TrackedStream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, fail=False):
        self.chunks = chunks
        self.fail = fail
        self.iterated = False
        self.closed = False

    async def __aiter__(self):
        self.iterated = True
        for chunk in self.chunks:
            yield chunk
        if self.fail:
            raise httpx.ReadError("upstream disconnected")

    async def aclose(self):
        self.closed = True


@asynccontextmanager
async def app_client(monkeypatch, handler, *, mode="throttled", fallback_mode="off", keys_count=1):
    state = ProxyState(
        []
        if mode == "no_keys"
        else ["nvapi-test", *[f"nvapi-extra-{i}" for i in range(keys_count - 1)]],
        ProxyStats(),
        lambda _: None,
    )
    if mode == "throttled":
        state.pool_throttled_until = time.time() + 60
    elif mode == "circuit":
        for _ in range(3):
            state.record_model_result(MODEL, status=503)
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    real_client = httpx.AsyncClient
    with monkeypatch.context() as patch:
        patch.setattr(proxy_app.httpx, "AsyncClient", lambda **_: upstream)
        app = proxy_app.create_app(state)
    monkeypatch.setattr(proxy_app, "load_provider_configs", lambda: [PROVIDER])
    monkeypatch.setattr(config, "model_options", lambda: {"fallback": fallback_mode})
    try:
        async with real_client(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:1919"
        ) as client:
            yield client, state
    finally:
        await upstream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,fallback_mode",
    [("throttled", "off"), ("no_keys", "off"), ("circuit", "off"), ("circuit", "auto")],
)
async def test_backup_serves_when_primary_cannot_accept_requests(monkeypatch, mode, fallback_mode):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "backup answer"}}]}
        )

    async with app_client(monkeypatch, handler, mode=mode, fallback_mode=fallback_mode) as (
        client,
        state,
    ):
        response = await client.post("/v1/chat/completions", json=PAYLOAD)
        assert response.status_code == 200
        assert response.headers["x-openvidia-provider"] == "backup"
        assert response.json()["choices"][0]["message"]["content"] == "backup answer"
        assert state.stats.success == 1
    assert [r.url.host for r in seen] == ["backup.example"]
    assert json.loads(seen[0].content)["model"] == "backup-model"
    assert seen[0].headers["authorization"] == "Bearer backup-key"


@pytest.mark.asyncio
async def test_invalid_primary_request_never_falls_back(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(400, json={"error": {"message": "invalid tool schema"}})

    async with app_client(monkeypatch, handler, mode="healthy") as (client, _):
        response = await client.post("/v1/chat/completions", json=PAYLOAD)
        assert response.status_code == 400
        assert "invalid tool schema" in response.json()["error"]
    assert seen == ["integrate.api.nvidia.com"]


@pytest.mark.asyncio
async def test_streaming_tool_call_and_tool_result_survive_provider_model_mapping(monkeypatch):
    seen = []
    streams = []
    tool_call = {
        "id": "call-1",
        "type": "function",
        "function": {"name": "read_file", "arguments": '{"path":"README.md"}'},
    }

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if body["messages"][-1]["role"] == "tool":
            delta = {"content": "File read successfully"}
            finish = "stop"
        else:
            delta = {"tool_calls": [{"index": 0, **tool_call}]}
            finish = "tool_calls"
        chunk = (
            "data: "
            + json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})
            + "\n\ndata: [DONE]\n\n"
        ).encode()
        stream = TrackedStream([chunk])
        streams.append(stream)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    async with app_client(monkeypatch, handler) as (client, _):
        payload = {
            **PAYLOAD,
            "stream": True,
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "read_file", "parameters": {"type": "object"}},
                }
            ],
        }
        first = await client.post("/v1/chat/completions", json=payload)
        assert first.headers["content-type"].startswith("text/event-stream")
        assert '"tool_calls"' in first.text
        assert "data: [DONE]" in first.text
        messages = [
            *PAYLOAD["messages"],
            {"role": "assistant", "content": None, "tool_calls": [tool_call]},
            {"role": "tool", "tool_call_id": "call-1", "content": "README contents"},
        ]
        second = await client.post("/v1/chat/completions", json={**payload, "messages": messages})
        assert "File read successfully" in second.text
    assert seen[1]["messages"] == messages
    assert seen[0]["tools"] == payload["tools"]
    assert all(x["stream"] and x["model"] == "backup-model" for x in seen)
    assert all(s.closed for s in streams)


@pytest.mark.asyncio
@pytest.mark.parametrize("primary", [False, True])
async def test_midstream_failure_closes_response_and_never_replays_tool_calls(monkeypatch, primary):
    seen = []
    stream = TrackedStream(
        [b'data: {"choices":[{"delta":{"tool_calls":[{"id":"call-side-effect"}]}}]}\n\n'], fail=True
    )

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    async with app_client(monkeypatch, handler, mode="healthy" if primary else "throttled") as (
        client,
        state,
    ):
        response = await client.post("/v1/chat/completions", json={**PAYLOAD, "stream": True})
        assert response.status_code == 200
        assert "call-side-effect" in response.text
        assert "stream interrupted" in response.text
        assert "[DONE]" not in response.text
        if primary:
            assert state.key_states["nvapi-test"].in_flight == 0
    assert seen == ["integrate.api.nvidia.com" if primary else "backup.example"]
    assert stream.closed


@pytest.mark.asyncio
async def test_try_fallback_stream_is_lazy_and_caller_closes_it():
    stream = TrackedStream([b"data: partial\n\n", b"data: [DONE]\n\n"])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
    ) as client:
        response = await try_fallback(client, {**PAYLOAD, "stream": True}, PROVIDER)
        assert response is not None
        assert not stream.iterated
        assert not response.is_closed
        chunks = []
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)
        await response.aclose()
        assert chunks == stream.chunks
        assert stream.closed


@pytest.mark.asyncio
async def test_rejected_backup_stream_is_closed_before_trying_another_provider():
    stream = TrackedStream([b"unavailable"])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503, stream=stream))
    ) as client:
        assert await try_fallback(client, {**PAYLOAD, "stream": True}, PROVIDER) is None
    assert stream.closed


@pytest.mark.asyncio
async def test_gateway_outage_spends_bounded_attempts_before_provider_failover(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "integrate.api.nvidia.com":
            return httpx.Response(504, json={"error": "gateway timeout"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "backup answer"}}]})

    async with app_client(monkeypatch, handler, mode="healthy", keys_count=12) as (client, _):
        response = await client.post("/v1/chat/completions", json=PAYLOAD)
        assert response.status_code == 200
        assert response.headers["x-openvidia-provider"] == "backup"
    assert seen.count("integrate.api.nvidia.com") == 5
    assert seen[-1] == "backup.example"


@pytest.mark.asyncio
async def test_disabled_provider_fallback_preserves_account_retry_after(monkeypatch):
    def handler(_):
        pytest.fail("Account throttling must prevent all upstream sends")

    async with app_client(monkeypatch, handler) as (client, _):
        monkeypatch.setattr(proxy_app, "load_provider_configs", lambda: [])
        response = await client.post("/v1/chat/completions", json=PAYLOAD)
        assert response.status_code == 429
        assert int(response.headers["retry-after"]) > 0


@pytest.mark.asyncio
async def test_failed_backup_moves_to_next_provider_before_any_output(monkeypatch):
    seen = []
    failed_stream = TrackedStream([b"unavailable"])

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "dead.example":
            return httpx.Response(503, stream=failed_stream)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=TrackedStream([b"data: [DONE]\n\n"]),
        )

    async with app_client(monkeypatch, handler) as (client, _):
        monkeypatch.setattr(
            proxy_app,
            "load_provider_configs",
            lambda: [ProviderConfig("dead", "https://dead.example/v1", "key"), PROVIDER],
        )
        response = await client.post("/v1/chat/completions", json={**PAYLOAD, "stream": True})
        assert response.text == "data: [DONE]\n\n"
        assert response.headers["x-openvidia-provider"] == "backup"
    assert seen == ["dead.example", "backup.example"]
    assert failed_stream.closed
