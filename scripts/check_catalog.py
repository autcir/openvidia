"""Check shipped tuning against the live catalog; never probe paid completions."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from openvidia.compaction import _DEFAULTS
from openvidia.config import _MODEL_OPTIONS_DEFAULTS, UPSTREAM_BASE


def main() -> int:
    response = httpx.get(UPSTREAM_BASE + "models", timeout=30)
    response.raise_for_status()
    live = {model["id"] for model in response.json()["data"]}
    shipped = set(_MODEL_OPTIONS_DEFAULTS["per_model"])
    if _DEFAULTS["summary_model"]:
        shipped.add(_DEFAULTS["summary_model"])
    missing = sorted(shipped - live)
    report = {"live_models": len(live), "shipped_tuning": sorted(shipped), "retired": missing}
    Path("catalog-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    for model in missing:
        print(f"::error::Retired model tuning: {model}")
    return int(bool(missing))


if __name__ == "__main__":
    raise SystemExit(main())
