"""The dashboard probe handles null/invalid provider answers without a 500."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from openvidia import config, proxy_app, webui
from openvidia.proxy_state import ProxyState, ProxyStats

MODEL = "vendor/probe"


@asynccontextmanager
async def probe_client(handler, *, keys=None):
    app = FastAPI()
    state = ProxyState(["test-key"] if keys is None else keys, ProxyStats(), lambda _line: None)
    app.post("/v1/chat/completions")(handler)
    webui.attach_webui(app, state, Path(webui.__file__).parent / "web")
    app.state.probe_transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:1919"
    ) as client:
        yield client, state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message,expected",
    [
        ({"content": None}, "empty_response"),
        ({"content": ""}, "empty_response"),
        ({"content": "   "}, "empty_response"),
        ({"content": None, "reasoning_content": "private reasoning"}, "reasoning_only"),
        ({"content": None, "reasoning": "private reasoning"}, "reasoning_only"),
        ({"content": None, "tool_calls": [{"id": "call_probe"}]}, "tool_only"),
        ({"content": {"bad": "format"}}, "invalid_response"),
    ],
)
async def test_null_reasoning_tool_only_and_invalid_content_are_not_success(message, expected):
    async def inference(request: Request):
        return {"choices": [{"message": message}]}

    async with probe_client(inference) as (client, _state):
        response = await client.post("/api/test-model", json={"model": MODEL})
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["error"] == expected
    assert "private reasoning" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body", [{}, {"choices": []}, {"choices": [None]}, {"choices": [{"message": None}]}, [], None]
)
async def test_malformed_completion_shapes_do_not_raise(body):
    async def inference(request: Request):
        return JSONResponse(body)

    async with probe_client(inference) as (client, _state):
        response = await client.post("/api/test-model", json={"model": MODEL})
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["error"] == "invalid_response"


@pytest.mark.asyncio
async def test_invalid_provider_json_never_leaks_raw_body():
    async def inference(request: Request):
        return Response("private-secret-not-json", media_type="application/json")

    async with probe_client(inference) as (client, _state):
        response = await client.post("/api/test-model", json={"model": MODEL})
    assert response.json()["error"] == "invalid_response"
    assert "private-secret" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected",
    [
        (400, "rejected"),
        (401, "authentication_failed"),
        (403, "authentication_failed"),
        (404, "rejected"),
        (429, "rate_limited"),
        (500, "unavailable"),
        (503, "unavailable"),
        (504, "timeout"),
    ],
)
async def test_upstream_rejections_are_safe_diagnostics(status, expected):
    async def inference(request: Request):
        return Response("nvapi-private-upstream-error", status_code=status)

    async with probe_client(inference) as (client, _state):
        response = await client.post("/api/test-model", json={"model": MODEL})
    assert response.status_code == 200
    assert response.json()["error"] == expected
    assert "nvapi-" not in response.text


@pytest.mark.asyncio
async def test_probe_deadline_bounds_all_internal_retries(monkeypatch):
    monkeypatch.setattr(webui, "MODEL_PROBE_TIMEOUT", 0.01)
    cancelled = asyncio.Event()

    async def inference(request: Request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with probe_client(inference) as (client, _state):
        response = await client.post("/api/test-model", json={"model": MODEL})
    assert response.json()["error"] == "timeout"
    assert cancelled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body", [None, [], "bad", {}, {"model": None}, {"model": []}, {"model": "bad model"}]
)
async def test_invalid_probe_body_is_a_client_error_without_inference(body):
    async def inference(request: Request):
        raise AssertionError("invalid request must not send inference")

    async with probe_client(inference) as (client, _state):
        response = await client.post(
            "/api/test-model",
            content=json.dumps(body),
            headers={"content-type": "application/json"},
        )
        malformed = await client.post("/api/test-model", content="{bad-json")
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"
    assert malformed.status_code == 400


@pytest.mark.asyncio
async def test_no_keys_is_diagnostic_and_text_success_preserves_selection():
    seen = []

    async def inference(request: Request):
        seen.append(await request.json())
        return {"choices": [{"message": {"content": "OK"}}]}

    async with probe_client(inference, keys=[]) as (client, _state):
        assert (await client.post("/api/test-model", json={"model": MODEL})).json()[
            "error"
        ] == "no_keys"
    async with probe_client(inference) as (client, state):
        state.active_model = "keep/active"
        result = (await client.post("/api/test-model", json={"model": MODEL})).json()
        assert state.active_model == "keep/active"
    assert result["ok"] is True
    assert result["output_chars"] == 2
    assert len(seen) == 1
    assert seen[0]["model"] == MODEL
    assert seen[0]["max_tokens"] == 256


@pytest.mark.asyncio
async def test_dashboard_probe_uses_actual_configured_proxy_and_tuning(monkeypatch):
    requests = []
    actual_client = httpx.AsyncClient

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    upstream = actual_client(transport=httpx.MockTransport(respond))
    state = ProxyState(["nvapi-test"], ProxyStats(), lambda _line: None)
    with monkeypatch.context() as patch:
        patch.setattr(proxy_app.httpx, "AsyncClient", lambda **_kwargs: upstream)
        patch.setattr(config, "upstream_endpoints", lambda: ["https://configured.example/v1/"])
        app = proxy_app.create_app(state, web_dir=Path(webui.__file__).parent / "web")
    app.state.probe_transport = httpx.ASGITransport(app=app)
    monkeypatch.setattr(
        config,
        "model_options",
        lambda: {"per_model": {MODEL: {"extra_payload": {"temperature": 0.25}}}},
    )
    try:
        async with actual_client(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:1919",
            headers={"x-openvidia-token": config.control_token()},
        ) as client:
            response = await client.post("/api/test-model", json={"model": MODEL})
        assert response.json()["ok"] is True
        assert len(requests) == 1
        assert requests[0].url.host == "configured.example"
        assert json.loads(requests[0].content)["temperature"] == 0.25
    finally:
        await upstream.aclose()
