"""Local, expiring evidence of streaming/tool checks, never model rankings."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import config
from .soak import ProbeError

TTL = timedelta(hours=6)
MAX_MODELS = 200
CHECKS = ("stream", "tool_call", "tool_result")


def _date(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("missing date")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("date requires timezone")
    return parsed.astimezone(UTC)


class ValidationStore:
    def __init__(self, path: Path | None = None):
        self.path = path if path is not None else config.config_dir() / "model-validation.json"

    def load(self) -> dict[str, dict]:
        try:
            if self.path.stat().st_size > 1024 * 1024:
                return {}
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not isinstance(raw.get("models"), dict):
                return {}
            result = {}
            now = datetime.now(UTC)
            for model, row in list(raw["models"].items())[:MAX_MODELS]:
                if not isinstance(model, str) or not config.is_valid_model_id(model):
                    continue
                if not isinstance(row, dict) or row.get("status") not in ("passed", "failed"):
                    continue
                try:
                    tested = _date(row.get("tested_at"))
                    expires = _date(row.get("expires_at"))
                except (ValueError, OverflowError):
                    continue
                if tested > now + timedelta(minutes=1) or not tested < expires <= tested + TTL:
                    continue
                clean = {
                    "status": row["status"],
                    "tested_at": tested.isoformat(),
                    "expires_at": expires.isoformat(),
                    **{key: row.get(key) is True for key in (*CHECKS, "opencode")},
                }
                reason = row.get("reason")
                if isinstance(reason, str) and (
                    reason in ProbeError.REASONS
                    or reason
                    in (
                        "timeout",
                        "http_error",
                        "invalid_response",
                        "opencode_failed",
                        "probe_failed",
                    )
                ):
                    clean["reason"] = reason
                if clean["status"] == "passed" and not all(clean[key] for key in CHECKS):
                    clean["status"] = "failed"
                    clean["reason"] = "invalid_response"
                result[model] = clean
            return result
        except (OSError, ValueError, TypeError, OverflowError):
            return {}

    def _save(self, models: dict[str, dict]) -> None:
        latest = sorted(models.items(), key=lambda item: item[1]["tested_at"], reverse=True)
        # Expiring evidence is a cache, not durable credentials. Atomic replace
        # prevents partial JSON without an fsync that can stall a busy disk.
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".model-validation-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(
                    {"schema_version": 1, "models": dict(latest[:MAX_MODELS])}, output, indent=2
                )
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def record(self, model: str, outcome: dict) -> dict:
        if not config.is_valid_model_id(model) or "/" not in model:
            raise ValueError("explicit model ID required")
        now = datetime.now(UTC)
        stages = outcome.get("stages") or {}
        checks = {key: stages.get(key) is True for key in CHECKS}
        passed = outcome.get("ok") is True and all(checks.values())
        row = {
            "status": "passed" if passed else "failed",
            "tested_at": now.isoformat(),
            "expires_at": (now + TTL).isoformat(),
            **checks,
            "opencode": False,
        }
        reason = outcome.get("reason")
        if reason in ProbeError.REASONS:
            row["reason"] = reason
        elif not passed:
            kind = outcome.get("error_type")
            row["reason"] = (
                "timeout"
                if kind in ("TimeoutError", "ReadTimeout")
                else "http_error"
                if kind == "HTTPStatusError"
                else "probe_failed"
            )
        models = self.load()
        models[model] = row
        self._save(models)
        return row

    def record_opencode(self, model: str, passed: bool) -> dict:
        """Only the local CLI operator can attest an actual OpenCode run."""
        models = self.load()
        row = models.get(model)
        if row is None or _date(row["expires_at"]) <= datetime.now(UTC):
            raise ValueError("fresh protocol evidence required first")
        row["opencode"] = passed is True and row["status"] == "passed"
        if not passed:
            row["status"] = "failed"
            row["reason"] = "opencode_failed"
        models[model] = row
        self._save(models)
        return row
