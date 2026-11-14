"""Duration-controlled live endurance monitor; never stores prompts or credentials.

Run with ``python -m openvidia.soak --hours 72 --output-dir /path/to/results``.
The default five-minute interval makes three tiny inference requests per cycle.
A completed run is evidence for that endpoint and model during that window only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import signal
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx


class ProbeError(Exception):
    """A completion violated a contract; reasons are a fixed safe vocabulary."""

    REASONS = frozenset(
        {
            "health_identity",
            "invalid_response",
            "provider_fallback",
            "stream_error",
            "stream_contract",
            "tool_call_count",
            "tool_call_id",
            "tool_call_name",
            "tool_arguments",
            "tool_result_contract",
        }
    )

    def __init__(self, reason: str):
        self.reason = reason if reason in self.REASONS else "probe_contract"
        super().__init__(self.reason)


def loopback_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise argparse.ArgumentTypeError("base URL must be plain loopback HTTP without credentials")
    try:
        _port = parsed.port
    except ValueError as error:
        raise argparse.ArgumentTypeError("invalid loopback port") from error
    return value.rstrip("/")


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _save(path: Path, data: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        os.chmod(temporary, 0o600)
        json.dump(data, output, indent=2)
        output.write("\n")
    temporary.replace(path)


def _response_object(response: httpx.Response) -> dict:
    try:
        value = response.json()
    except ValueError as error:
        raise ProbeError("invalid_response") from error
    if not isinstance(value, dict):
        raise ProbeError("invalid_response")
    return value


def _require_direct_model(response: httpx.Response) -> None:
    # A successful fallback proves the fallback works, not the requested
    # model. Never persist the provider name or raw response as a reason.
    if "x-openvidia-provider" in response.headers:
        raise ProbeError("provider_fallback")


def _response_message(response: httpx.Response) -> dict:
    _require_direct_model(response)
    data = _response_object(response)
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ProbeError("invalid_response")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ProbeError("invalid_response")
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise ProbeError("invalid_response")
    return message


async def probe(client: httpx.AsyncClient, model: str, timeout: float) -> dict:
    """Check liveness, SSE termination and an actual tool-result round trip."""
    results = {"health": False, "stream": False, "tool_call": False, "tool_result": False}
    try:
        async with asyncio.timeout(timeout):
            response = await client.get("/health")
            response.raise_for_status()
            if _response_object(response).get("service") != "openvidia":
                raise ProbeError("health_identity")
            results["health"] = True
            probe_headers = {"X-OpenVidia-Strict-Model": "1"}
            common = {
                "model": model,
                "max_tokens": 128,
                "chat_template_kwargs": {"enable_thinking": False},
            }
            async with client.stream(
                "POST",
                "/v1/chat/completions",
                headers=probe_headers,
                json={
                    **common,
                    "stream": True,
                    "messages": [{"role": "user", "content": "Reply exactly SOAK_OK."}],
                },
            ) as response:
                response.raise_for_status()
                _require_direct_model(response)
                content = []
                done = False
                async for line in response.aiter_lines():
                    if line == "data: [DONE]":
                        done = True
                        break
                    if line.startswith("data: "):
                        try:
                            event = json.loads(line[6:])
                        except ValueError as error:
                            raise ProbeError("invalid_response") from error
                        if not isinstance(event, dict):
                            raise ProbeError("invalid_response")
                        if "error" in event:
                            raise ProbeError("stream_error")
                        choices = event.get("choices", [])
                        if not isinstance(choices, list):
                            raise ProbeError("invalid_response")
                        for choice in choices:
                            if not isinstance(choice, dict):
                                raise ProbeError("invalid_response")
                            delta = choice.get("delta")
                            if not isinstance(delta, dict):
                                raise ProbeError("invalid_response")
                            text = delta.get("content")
                            if text is not None and not isinstance(text, str):
                                raise ProbeError("invalid_response")
                            content.append(text or "")
                if not done or "".join(content).strip() != "SOAK_OK":
                    raise ProbeError("stream_contract")
            results["stream"] = True
            messages = [
                {
                    "role": "user",
                    "content": "Call soak_echo with value SOAK_TOOL_OK. After its result, reply exactly SOAK_TOOL_OK.",
                }
            ]
            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "soak_echo",
                        "description": "Return the supplied probe value",
                        "parameters": {
                            "type": "object",
                            "properties": {"value": {"type": "string", "enum": ["SOAK_TOOL_OK"]}},
                            "required": ["value"],
                            "additionalProperties": False,
                        },
                    },
                }
            ]
            response = await client.post(
                "/v1/chat/completions",
                headers=probe_headers,
                json={**common, "messages": messages, "tools": tools, "tool_choice": "required"},
            )
            response.raise_for_status()
            message = _response_message(response)
            calls = message.get("tool_calls", [])
            if not isinstance(calls, list) or not 1 <= len(calls) <= 16:
                raise ProbeError("tool_call_count")
            call_ids = set()
            for call in calls:
                if not isinstance(call, dict):
                    raise ProbeError("tool_call_id")
                call_id = call.get("id")
                if not isinstance(call_id, str) or not call_id.strip() or call_id in call_ids:
                    raise ProbeError("tool_call_id")
                call_ids.add(call_id)
                function = call.get("function")
                if (
                    call.get("type") != "function"
                    or not isinstance(function, dict)
                    or function.get("name") != "soak_echo"
                ):
                    raise ProbeError("tool_call_name")
                try:
                    arguments = json.loads(function.get("arguments", ""))
                except (ValueError, TypeError) as error:
                    raise ProbeError("tool_arguments") from error
                if arguments != {"value": "SOAK_TOOL_OK"}:
                    raise ProbeError("tool_arguments")
            results["tool_call"] = True
            messages.append(
                {
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": calls,
                }
            )
            messages.extend(
                {"role": "tool", "tool_call_id": call["id"], "content": "SOAK_TOOL_OK"}
                for call in calls
            )
            response = await client.post(
                "/v1/chat/completions",
                headers=probe_headers,
                json={**common, "messages": messages, "tools": tools, "tool_choice": "none"},
            )
            response.raise_for_status()
            if (_response_message(response).get("content") or "").strip() != "SOAK_TOOL_OK":
                raise ProbeError("tool_result_contract")
            results["tool_result"] = True
    except (
        httpx.HTTPError,
        TimeoutError,
        ValueError,
        KeyError,
        IndexError,
        TypeError,
        ProbeError,
    ) as error:
        # Exception messages and provider bodies may contain sensitive data.
        failure = {"ok": False, "stages": results, "error_type": type(error).__name__}
        if isinstance(error, ProbeError):
            failure["reason"] = error.reason
        return failure
    return {"ok": True, "stages": results}


async def monitor(
    *,
    base_url: str,
    model: str,
    hours: float,
    interval: float,
    timeout: float,
    output_dir: Path,
    max_failures: int,
    stop: asyncio.Event | None = None,
) -> int:
    """Persist every outcome; a signal or partial duration can never pass."""
    stop = stop or asyncio.Event()
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    summary_path = output_dir / "summary.json"
    if summary_path.exists():
        raise FileExistsError("results already exist; choose a new output directory")
    target = hours * 3600
    started = time.monotonic()
    summary = {
        "status": "running",
        "started_at": _timestamp(),
        "base_url": base_url,
        "model": model,
        "target_seconds": target,
        "elapsed_seconds": 0.0,
        "interval_seconds": interval,
        "cycles": 0,
        "successful_cycles": 0,
        "failed_cycles": 0,
        "max_failures": max_failures,
    }
    _save(summary_path, summary)
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=timeout, trust_env=False) as client:
            while not stop.is_set():
                cycle_started = time.monotonic()
                if cycle_started - started >= target:
                    break
                outcome = await probe(client, model, timeout)
                row = {
                    "timestamp": _timestamp(),
                    "duration_seconds": round(time.monotonic() - cycle_started, 3),
                    **outcome,
                }
                with (output_dir / "probes.jsonl").open("a", encoding="utf-8") as log:
                    os.chmod(output_dir / "probes.jsonl", 0o600)
                    log.write(json.dumps(row) + "\n")
                summary["cycles"] += 1
                summary["successful_cycles" if outcome["ok"] else "failed_cycles"] += 1
                summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
                _save(summary_path, summary)
                remaining = target - (time.monotonic() - started)
                if remaining > 0:
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=min(interval, remaining))
                    except TimeoutError:
                        pass
    except asyncio.CancelledError:
        stop.set()
    except Exception:
        summary["status"] = "error"
        raise
    finally:
        elapsed = time.monotonic() - started
        summary["elapsed_seconds"] = round(elapsed, 3)
        summary["finished_at"] = _timestamp()
        if summary["status"] != "error":
            if stop.is_set() or elapsed < target:
                summary["status"] = "interrupted"
            else:
                summary["status"] = (
                    "passed"
                    if summary["cycles"] > 0 and summary["failed_cycles"] <= max_failures
                    else "failed"
                )
        _save(summary_path, summary)
    return 0 if summary["status"] == "passed" else 130 if summary["status"] == "interrupted" else 1


async def _run(args) -> int:
    stop = asyncio.Event()
    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, lambda *_: stop.set())
    try:
        return await monitor(**vars(args), stop=stop)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", type=loopback_url, default="http://127.0.0.1:1919")
    parser.add_argument("--model", default="openvidia")
    parser.add_argument("--hours", type=float, default=72)
    parser.add_argument(
        "--interval", type=float, default=300, help="seconds between cycles; recommend at least 60"
    )
    parser.add_argument(
        "--timeout", type=float, default=180, help="upper bound for one complete cycle"
    )
    parser.add_argument("--max-failures", type=int, default=0)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if (
        not all(math.isfinite(value) for value in (args.hours, args.interval, args.timeout))
        or args.hours <= 0
        or args.interval < 1
        or args.timeout <= 0
        or args.max_failures < 0
    ):
        parser.error("hours/timeout must be positive, interval >= 1, max-failures >= 0")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
