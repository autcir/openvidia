"""Selected live checks preserve configuration and produce sanitized evidence."""

from __future__ import annotations

import argparse
import asyncio
import json

import httpx
import pytest

from openvidia import validate_models as validation


def handler(*, reported_model=None, calls=None, fail=False):
    def respond(request):
        if calls is not None:
            calls.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"service": "openvidia"})
        payload = json.loads(request.content)
        if fail:
            return httpx.Response(503, text="secret-provider-error-body")
        if payload.get("stream"):
            return httpx.Response(
                200, text='data: {"choices":[{"delta":{"content":"SOAK_OK"}}]}\n\ndata: [DONE]\n\n'
            )
        model = reported_model or payload["model"]
        if payload.get("tool_choice") == "required":
            return httpx.Response(
                200,
                json={
                    "model": model,
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "call_probe",
                                        "type": "function",
                                        "function": {
                                            "name": "soak_echo",
                                            "arguments": '{"value":"SOAK_TOOL_OK"}',
                                        },
                                    }
                                ]
                            }
                        }
                    ],
                },
            )
        return httpx.Response(
            200, json={"model": model, "choices": [{"message": {"content": "SOAK_TOOL_OK"}}]}
        )

    return respond


@pytest.mark.asyncio
async def test_validator_selects_explicit_model_without_mutating_active_model(
    tmp_path, isolated_config_dir
):
    active = isolated_config_dir / "active_model"
    active.write_text("keep/selected")
    calls = []
    output = tmp_path / "report.json"
    report = await validation.validate_models(
        models=["test/selected", "test/selected"],
        base_url="http://127.0.0.1:1919",
        timeout=5,
        concurrency=1,
        output=output,
        transport=httpx.MockTransport(handler(calls=calls)),
    )
    assert report["status"] == "passed"
    assert len(report["results"]) == 1
    row = report["results"][0]
    assert row["declared_model"] == "test/selected"
    assert row["response_models"] == ["test/selected"]
    assert row["identity"] == "matched"
    assert all(row["stages"].values())
    assert row["latency_seconds"] >= 0
    assert active.read_text() == "keep/selected"
    assert [str(request.url.path) for request in calls] == [
        "/health",
        "/v1/chat/completions",
        "/v1/chat/completions",
        "/v1/chat/completions",
    ]
    assert json.loads(output.read_text())["status"] == "passed"
    assert "messages" not in output.read_text()


@pytest.mark.asyncio
async def test_response_model_mismatch_is_explicit_evidence(tmp_path):
    report = await validation.validate_models(
        models=["test/requested"],
        base_url="http://localhost:1919",
        timeout=5,
        concurrency=1,
        output=tmp_path / "report.json",
        transport=httpx.MockTransport(handler(reported_model="other/response")),
    )
    assert report["results"][0]["identity"] == "different"
    assert report["results"][0]["response_models"] == ["other/response"]


@pytest.mark.asyncio
async def test_provider_error_body_is_never_saved(tmp_path):
    output = tmp_path / "report.json"
    report = await validation.validate_models(
        models=["test/requested"],
        base_url="http://localhost:1919",
        timeout=5,
        concurrency=1,
        output=output,
        transport=httpx.MockTransport(handler(fail=True)),
    )
    assert report["status"] == "failed"
    assert report["results"][0]["error_type"] == "HTTPStatusError"
    assert "secret-provider" not in output.read_text()
    assert "SOAK_OK" not in output.read_text()


@pytest.mark.asyncio
async def test_concurrency_never_exceeds_selected_bound(tmp_path, monkeypatch):
    active = 0
    peak = 0

    async def probe(client, model, timeout):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"ok": True, "stages": {}}

    monkeypatch.setattr(validation, "probe", probe)
    report = await validation.validate_models(
        models=[f"test/model-{index}" for index in range(5)],
        base_url="http://localhost:1919",
        timeout=5,
        concurrency=2,
        output=tmp_path / "report.json",
    )
    assert report["status"] == "passed"
    assert peak == 2


@pytest.mark.asyncio
async def test_interruption_records_unexecuted_checks_and_never_passes(tmp_path, monkeypatch):
    began = asyncio.Event()

    async def probe(*args):
        began.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(validation, "probe", probe)
    output = tmp_path / "report.json"
    task = asyncio.create_task(
        validation.validate_models(
            models=["test/one", "test/two"],
            base_url="http://localhost:1919",
            timeout=5,
            concurrency=1,
            output=output,
        )
    )
    await began.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    report = json.loads(output.read_text())
    assert report["status"] == "interrupted"
    assert [row["status"] for row in report["results"]] == ["interrupted", "not_run"]


@pytest.mark.asyncio
async def test_preexisting_report_is_preserved(tmp_path):
    output = tmp_path / "report.json"
    output.write_text("existing evidence")
    with pytest.raises(FileExistsError):
        await validation.validate_models(
            models=["test/model"],
            base_url="http://localhost:1919",
            timeout=5,
            concurrency=1,
            output=output,
        )
    assert output.read_text() == "existing evidence"


@pytest.mark.parametrize(
    "model", ["openvidia", "nvapi-secret", "sk-secret", "bad model/id", "http://evil"]
)
def test_explicit_model_argument_rejects_aliases_and_credentials(model):
    with pytest.raises(argparse.ArgumentTypeError):
        validation.explicit_model(model)
