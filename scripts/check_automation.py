"""Check GitHub automation from an independent host; never send notifications."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path


class GitHubAPIError(Exception):
    """A sanitized GitHub API failure, without command output or credentials."""


def gh_json(endpoint: str):
    try:
        result = subprocess.run(
            ["gh", "api", endpoint], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        raise GitHubAPIError("GitHub API unavailable or timed out") from None
    if result.returncode != 0:
        raise GitHubAPIError("GitHub API request failed; check gh authentication and permissions")
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError):
        raise GitHubAPIError("GitHub API returned invalid JSON") from None


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp requires a timezone")
    return parsed.astimezone(UTC)


def run_summary(run: dict | None):
    if run is None:
        return None
    # Do not persist arbitrary server text, URLs or inputs in the report.
    return {
        "id": run.get("id"),
        "event": run.get("event"),
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
    }


def inspect_automation(
    repository: str = "autcir/openvidia",
    *,
    runner_name: str = "soliso-autcir-openvidia",
    now: datetime | None = None,
    max_age_hours: float = 48,
    schedule_grace_hours: float = 2,
    api=gh_json,
) -> dict:
    """Verify runner and catalog freshness, including missed daily 06:17 UTC runs."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    report = {
        "checked_at": now.isoformat(),
        "repository": repository,
        "healthy": False,
        "errors": [],
        "runner": {"name": runner_name, "online": False},
        "catalog": {"max_age_hours": max_age_hours, "missed_scheduled_run": None},
    }
    try:
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
            raise ValueError("Invalid repository")
        if max_age_hours <= 0 or schedule_grace_hours < 0:
            raise ValueError("Invalid freshness or grace interval")
        repo = api(f"repos/{repository}")
        branch = repo["default_branch"]
        if not isinstance(branch, str) or not branch:
            raise ValueError("Missing default branch")
        report["default_branch"] = branch
        runners = api(f"repos/{repository}/actions/runners?per_page=100")["runners"]
        matching = [r for r in runners if r.get("name") == runner_name]
        report["runner"]["online"] = any(r.get("status") == "online" for r in matching)
        if not report["runner"]["online"]:
            report["errors"].append("Required GitHub Actions runner is offline or missing")
        # Filter locally too: API branch filtering alone must not let a pull
        # request or feature-branch success hide broken default-branch automation.
        from urllib.parse import quote

        raw = api(
            f"repos/{repository}/actions/workflows/catalog.yml/runs"
            f"?branch={quote(branch, safe='')}&per_page=100"
        )["workflow_runs"]
        runs = [
            r for r in raw if r.get("head_branch") == branch and r.get("event") != "pull_request"
        ]
        runs.sort(key=lambda r: parse_time(r["created_at"]), reverse=True)
        completed = [r for r in runs if r.get("status") == "completed"]
        completed.sort(key=lambda r: parse_time(r["updated_at"]), reverse=True)
        latest = completed[0] if completed else None
        successful = [r for r in completed if r.get("conclusion") == "success"]
        successful.sort(key=lambda r: parse_time(r["updated_at"]), reverse=True)
        success = successful[0] if successful else None
        catalog = report["catalog"]
        catalog["latest_completed"] = run_summary(latest)
        catalog["last_success"] = run_summary(success)
        if latest is None or latest.get("conclusion") != "success":
            report["errors"].append("Latest completed default-branch catalog check did not succeed")
        age = (now - parse_time(success["updated_at"])).total_seconds() / 3600 if success else None
        catalog["success_age_hours"] = round(age, 2) if age is not None else None
        if age is None or age > max_age_hours or age < 0:
            report["errors"].append("No successful catalog check within the freshness window")
        expected = now.replace(hour=6, minute=17, second=0, microsecond=0)
        if now < expected + timedelta(hours=schedule_grace_hours):
            expected -= timedelta(days=1)
        catalog["expected_schedule_at"] = expected.isoformat()
        catalog["schedule_grace_hours"] = schedule_grace_hours
        scheduled = [r for r in runs if r.get("event") == "schedule"]
        catalog["last_scheduled"] = run_summary(scheduled[0] if scheduled else None)
        scheduled_success = any(
            r.get("status") == "completed"
            and r.get("conclusion") == "success"
            and parse_time(r["created_at"]) >= expected
            and parse_time(r["created_at"]) <= now
            for r in scheduled
        )
        catalog["missed_scheduled_run"] = not scheduled_success
        if not scheduled_success:
            report["errors"].append(
                "Expected daily catalog run did not complete successfully within its grace period"
            )
    except GitHubAPIError as exc:
        report["errors"].append(str(exc))
    except (KeyError, TypeError, ValueError, AttributeError):
        report["errors"].append("Invalid repository configuration or GitHub API response")
    report["healthy"] = not report["errors"]
    return report


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(report, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default="autcir/openvidia")
    parser.add_argument("--runner", default="soliso-autcir-openvidia")
    parser.add_argument(
        "--output", type=Path, required=True, help="Persistent JSON report destination"
    )
    parser.add_argument("--max-age-hours", type=float, default=48)
    parser.add_argument("--schedule-grace-hours", type=float, default=2)
    args = parser.parse_args(argv)
    report = inspect_automation(
        args.repository,
        runner_name=args.runner,
        max_age_hours=args.max_age_hours,
        schedule_grace_hours=args.schedule_grace_hours,
    )
    try:
        write_report(args.output, report)
    except OSError:
        print("Automation check could not persist its report")
        return 1
    print(
        f"Automation {'healthy' if report['healthy'] else 'unhealthy'}; {len(report['errors'])} problem(s); report: {args.output}"
    )
    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
