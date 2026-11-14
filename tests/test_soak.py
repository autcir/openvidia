"""Endurance results distinguish duration, interruption and real model contracts."""

from __future__ import annotations

import argparse
import asyncio
import json

import httpx
import pytest

from openvidia import soak


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1",
        "http://remote.example",
        "http://localhost@evil.test",
        "http://localhost/?token=secret",
        "http://127.0.0.1/x",
        "http://localhost:bad",
    ],
)
def test_remote_or_credential_urls_are_rejected(url):
    with pytest.raises(argparse.ArgumentTypeError):
        soak.loopback_url(url)


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:1919", "http://[::1]:1919", "http://localhost:1919/"]
)
def test_only_plain_loopback_urls_are_accepted(url):
    assert soak.loopback_url(url) == url.rstrip("/")


def _handler(*, done=True, tool_result="SOAK_TOOL_OK"):
    def respond(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"service": "openvidia"})
        payload = json.loads(request.content)
        if payload.get("stream"):
            body = 'data: {"choices":[{"delta":{"content":"SOAK_OK"}}]}\n\n'
            if done:
                body += "data: [DONE]\n\n"
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
        if payload.get("tool_choice") == "required":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_probe",
                                        "type": "function",
                                        "function": {
                                            "name": "soak_echo",
                                            "arguments": '{"value":"SOAK_TOOL_OK"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        assert payload["messages"][-1] == {
            "role": "tool",
            "tool_call_id": "call_probe",
            "content": "SOAK_TOOL_OK",
        }
        assert payload["messages"][-2]["content"] == ""
        return httpx.Response(200, json={"choices": [{"message": {"content": tool_result}}]})

    return respond


@pytest.mark.asyncio
async def test_live_probe_requires_sse_and_complete_tool_roundtrip():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler()), base_url="http://localhost:1919"
    ) as client:
        result = await soak.probe(client, "test/model", 2)
    assert result["ok"] is True
    assert all(result["stages"].values())


@pytest.mark.asyncio
async def test_truncated_stream_is_a_failure_even_with_expected_text():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler(done=False)), base_url="http://localhost:1919"
    ) as client:
        result = await soak.probe(client, "test/model", 2)
    assert result["ok"] is False
    assert result["stages"]["stream"] is False


@pytest.mark.asyncio
async def test_tool_result_must_be_used_not_just_called():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler(tool_result="ignored")),
        base_url="http://localhost:1919",
    ) as client:
        result = await soak.probe(client, "test/model", 2)
    assert result["ok"] is False
    assert result["stages"]["tool_call"] is True
    assert result["stages"]["tool_result"] is False


@pytest.mark.asyncio
async def test_interrupted_duration_can_never_pass_and_does_not_store_errors(tmp_path, monkeypatch):
    stop = asyncio.Event()

    async def probe(*args):
        stop.set()
        return {"ok": True, "stages": {"health": True}}

    monkeypatch.setattr(soak, "probe", probe)
    code = await soak.monitor(
        base_url="http://127.0.0.1:1919",
        model="openvidia",
        hours=72,
        interval=300,
        timeout=10,
        output_dir=tmp_path,
        max_failures=0,
        stop=stop,
    )
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert code == 130
    assert summary["status"] == "interrupted"
    assert summary["elapsed_seconds"] < summary["target_seconds"]
    assert "messages" not in (tmp_path / "probes.jsonl").read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("ok,expected", [(True, "passed"), (False, "failed")])
async def test_full_duration_and_failure_threshold_determine_result(
    tmp_path, monkeypatch, ok, expected
):
    # Measure controlled duration, not the speed of SSL/client setup on the
    # CI host. A 3.6ms wall deadline could expire before the first probe.
    now = [0.0]
    monkeypatch.setattr(soak.time, "monotonic", lambda: now[0])

    async def probe(*args):
        now[0] = 1.0
        return {"ok": ok, "stages": {"health": ok}}

    monkeypatch.setattr(soak, "probe", probe)
    code = await soak.monitor(
        base_url="http://127.0.0.1:1919",
        model="openvidia",
        hours=0.5 / 3600,
        interval=1,
        timeout=1,
        output_dir=tmp_path,
        max_failures=0,
    )
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["status"] == expected
    assert code == (0 if ok else 1)
    assert summary["elapsed_seconds"] >= summary["target_seconds"]


@pytest.mark.asyncio
async def test_existing_results_are_never_overwritten(tmp_path):
    (tmp_path / "summary.json").write_text('{"status":"passed"}')
    with pytest.raises(FileExistsError):
        await soak.monitor(
            base_url="http://127.0.0.1:1919",
            model="openvidia",
            hours=72,
            interval=300,
            timeout=10,
            output_dir=tmp_path,
            max_failures=0,
        )


@pytest.mark.asyncio
async def test_multiple_parallel_calls_get_individual_results():
    initial_handler = _handler()

    def handler(request):
        if request.url.path == "/health":
            return initial_handler(request)
        payload = json.loads(request.content)
        if payload.get("stream"):
            return initial_handler(request)
        if payload.get("tool_choice") == "required":
            calls = [
                {
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {"name": "soak_echo", "arguments": '{"value":"SOAK_TOOL_OK"}'},
                }
                for i in range(2)
            ]
            return httpx.Response(200, json={"choices": [{"message": {"tool_calls": calls}}]})
        assert [
            message["tool_call_id"] for message in payload["messages"] if message["role"] == "tool"
        ] == ["call_0", "call_1"]
        assert all(
            message["content"] == "SOAK_TOOL_OK"
            for message in payload["messages"]
            if message["role"] == "tool"
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": "SOAK_TOOL_OK"}}]})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://localhost:1919"
    ) as client:
        result = await soak.probe(client, "test/model", 2)
    assert result["ok"] is True
    assert all(result["stages"].values())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "count,duplicate,expected",
    [(0, False, "tool_call_count"), (17, False, "tool_call_count"), (2, True, "tool_call_id")],
)
async def test_invalid_parallel_calls_report_only_static_reasons(count, duplicate, expected):
    initial_handler = _handler()

    def handler(request):
        if request.url.path == "/health" or json.loads(request.content).get("stream"):
            return initial_handler(request)
        calls = [
            {
                "id": "duplicate" if duplicate else f"call_{i}",
                "type": "function",
                "function": {"name": "soak_echo", "arguments": '{"value":"SOAK_TOOL_OK"}'},
            }
            for i in range(count)
        ]
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": calls}}]})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://localhost:1919"
    ) as client:
        result = await soak.probe(client, "test/model", 2)
    assert result["ok"] is False
    assert result["reason"] == expected
    assert set(result) == {"ok", "stages", "error_type", "reason"}


def test_probe_error_never_keeps_unknown_provider_text():
    error = soak.ProbeError("secret-provider-content")
    assert error.reason == "probe_contract"
    assert "secret" not in str(error)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage,malformed",
    [
        ("health", []),
        ("stream", []),
        ("stream", {"choices": "bad"}),
        ("stream", {"choices": ["bad"]}),
        ("stream", {"choices": [{"delta": "bad"}]}),
        ("stream", {"choices": [{"delta": {"content": ["bad"]}}]}),
        ("tool_call", []),
        ("tool_call", {"choices": {}}),
        ("tool_call", {"choices": ["bad"]}),
        ("tool_call", {"choices": [{"message": "bad"}]}),
        ("tool_result", {"choices": [{"message": {"content": ["bad"]}}]}),
    ],
)
async def test_malformed_response_shapes_fail_safely_instead_of_raising(stage, malformed):
    original = _handler()

    def handler(request):
        payload = {} if request.url.path == "/health" else json.loads(request.content)
        current = (
            "health"
            if request.url.path == "/health"
            else "stream"
            if payload.get("stream")
            else "tool_call"
            if payload.get("tool_choice") == "required"
            else "tool_result"
        )
        if current != stage:
            return original(request)
        if current == "stream":
            return httpx.Response(
                200,
                text=f"data: {json.dumps(malformed)}\n\ndata: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=malformed)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://localhost:1919"
    ) as client:
        result = await soak.probe(client, "test/model", 2)
    assert result["ok"] is False
    assert result["reason"] == "invalid_response"
    assert result["stages"][stage] is False
    assert set(result) == {"ok", "stages", "error_type", "reason"}


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["stream", "tool_call", "tool_result"])
async def test_provider_fallback_cannot_certify_requested_model(stage):
    original = _handler()

    def handler(request):
        response = original(request)
        if request.url.path != "/health":
            payload = json.loads(request.content)
            current = (
                "stream"
                if payload.get("stream")
                else "tool_call"
                if payload.get("tool_choice") == "required"
                else "tool_result"
            )
            if current == stage:
                response.headers["X-OpenVidia-Provider"] = "secret-provider-name"
        return response

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://localhost:1919"
    ) as client:
        result = await soak.probe(client, "test/model", 2)
    assert result["ok"] is False
    assert result["reason"] == "provider_fallback"
    assert result["stages"][stage] is False
    assert "secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_protocol_probe_disables_proxy_fallback_for_every_completion():
    original = _handler()
    checked = []

    def handler(request):
        if request.url.path != "/health":
            assert request.headers["X-OpenVidia-Strict-Model"] == "1"
            checked.append(request)
        return original(request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://localhost:1919"
    ) as client:
        result = await soak.probe(client, "test/model", 2)
    assert result["ok"] is True
    assert len(checked) == 3


@pytest.mark.asyncio
async def test_probe_cancellation_does_not_become_failed_evidence():
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def handler(request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://localhost:1919"
    ) as client:
        task = asyncio.create_task(soak.probe(client, "test/model", 60))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()
