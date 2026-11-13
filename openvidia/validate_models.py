"""Opt-in live protocol checks for an explicit selection of NVIDIA model IDs.

This exercises the local OpenVidia HTTP endpoint without changing its active
model. Each selected model makes three small inference requests; real provider
quotas apply. Results contain no prompts, tool arguments, credentials or bodies.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .config import is_valid_model_id
from .soak import ProbeError, loopback_url, probe


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def explicit_model(value: str) -> str:
    if not is_valid_model_id(value) or "/" not in value or value.startswith(("nvapi-", "sk-")):
        raise argparse.ArgumentTypeError(
            "select an explicit provider/model ID, not the active-model alias"
        )
    return value


def _write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        os.chmod(temporary, 0o600)
        json.dump(report, output, indent=2)
        output.write("\n")
    temporary.replace(path)


class EvidenceClient(httpx.AsyncClient):
    """Retain only validated model IDs from buffered completion responses."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.response_models: list[str] = []

    async def post(self, *args, **kwargs) -> httpx.Response:
        response = await super().post(*args, **kwargs)
        try:
            model = response.json().get("model")
            if (
                isinstance(model, str)
                and is_valid_model_id(model)
                and not model.startswith(("nvapi-", "sk-"))
                and model not in self.response_models
            ):
                self.response_models.append(model)
        except (ValueError, AttributeError):
            pass
        return response


async def validate_models(
    *,
    models: list[str],
    base_url: str,
    timeout: float,
    concurrency: int,
    output: Path,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """Write incremental reports, retaining interrupted or unexecuted checks."""
    if output.exists():
        raise FileExistsError("report already exists; choose a new output path")
    models = list(dict.fromkeys(explicit_model(model) for model in models))
    if not models or not math.isfinite(timeout) or timeout <= 0 or concurrency not in (1, 2):
        raise ValueError("select models, a finite positive timeout and concurrency 1 or 2")
    base_url = loopback_url(base_url)
    started = time.monotonic()
    report = {
        "schema_version": 1,
        "status": "running",
        "started_at": _timestamp(),
        "base_url": base_url,
        "timeout_seconds": timeout,
        "concurrency": concurrency,
        "probe": "stream_done_and_tool_result_roundtrip",
        "scope": "explicit_selection",
        "results": [{"declared_model": model, "status": "not_run"} for model in models],
    }
    _write_report(output, report)
    semaphore = asyncio.Semaphore(concurrency)

    async def check(index: int, model: str) -> None:
        async with semaphore:
            began = time.monotonic()
            row = {"declared_model": model, "started_at": _timestamp(), "status": "running"}
            report["results"][index] = row
            _write_report(output, report)
            reported_models = []
            try:
                async with EvidenceClient(
                    base_url=base_url, timeout=timeout, trust_env=False, transport=transport
                ) as client:
                    outcome = await probe(client, model, timeout)
                    reported_models = client.response_models
                row["status"] = "passed" if outcome.get("ok") is True else "failed"
                row["stages"] = {
                    stage: outcome.get("stages", {}).get(stage) is True
                    for stage in ("health", "stream", "tool_call", "tool_result")
                }
                error_type = outcome.get("error_type")
                if (
                    isinstance(error_type, str)
                    and error_type.isidentifier()
                    and len(error_type) <= 80
                ):
                    row["error_type"] = error_type
                reason = outcome.get("reason")
                if reason in ProbeError.REASONS or reason == "probe_contract":
                    row["reason"] = reason
            except asyncio.CancelledError:
                row.update(
                    status="interrupted",
                    finished_at=_timestamp(),
                    latency_seconds=round(time.monotonic() - began, 3),
                )
                _write_report(output, report)
                raise
            except Exception as error:
                row.update(status="failed", error_type=type(error).__name__)
            row["response_models"] = reported_models
            row["identity"] = (
                "not_reported"
                if not reported_models
                else "matched"
                if all(value == model for value in reported_models)
                else "different"
            )
            row["latency_seconds"] = round(time.monotonic() - began, 3)
            row["finished_at"] = _timestamp()
            report["results"][index] = row
            _write_report(output, report)

    completed = False
    try:
        await asyncio.gather(*(check(index, model) for index, model in enumerate(models)))
        completed = True
    finally:
        report["status"] = (
            "interrupted"
            if not completed
            else "passed"
            if all(row["status"] == "passed" for row in report["results"])
            else "failed"
        )
        report["finished_at"] = _timestamp()
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["passed"] = sum(row["status"] == "passed" for row in report["results"])
        report["failed"] = sum(row["status"] == "failed" for row in report["results"])
        _write_report(output, report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="+", type=explicit_model, required=True)
    parser.add_argument("--base-url", type=loopback_url, default="http://127.0.0.1:1919")
    parser.add_argument(
        "--timeout", type=float, default=120, help="maximum seconds for one complete model cycle"
    )
    parser.add_argument("--concurrency", type=int, choices=(1, 2), default=1)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new JSON report path; existing reports are preserved",
    )
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("timeout must be finite and positive")
    if args.output.exists():
        parser.error("report already exists; choose a new output path")
    try:
        report = asyncio.run(validate_models(**vars(args)))
    except KeyboardInterrupt:
        return 130
    print(
        f"{report['status']}: {report['passed']}/{len(report['results'])} protocol checks passed; report: {args.output}"
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
