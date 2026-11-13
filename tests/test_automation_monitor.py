"""The independent monitor must notice stopped automation and sanitize errors."""

import importlib.util
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "check_automation", Path(__file__).resolve().parents[1] / "scripts/check_automation.py"
)
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def run(*, age=5, branch="main", event="schedule", conclusion="success", status="completed"):
    when = (NOW - timedelta(hours=age)).isoformat()
    return {
        "id": 1,
        "created_at": when,
        "updated_at": when,
        "head_branch": branch,
        "event": event,
        "conclusion": conclusion,
        "status": status,
    }


def api_for(runs, *, online=True):
    def api(endpoint):
        if "actions/runners" in endpoint:
            return {
                "runners": [
                    {"name": "soliso-autcir-openvidia", "status": "online" if online else "offline"}
                ]
            }
        if "workflows/catalog.yml/runs" in endpoint:
            return {"workflow_runs": runs}
        return {"default_branch": "main"}

    return api


def test_recent_scheduled_success_and_online_runner_are_healthy():
    report = monitor.inspect_automation(now=NOW, api=api_for([run()]))
    assert report["healthy"]
    assert report["catalog"]["missed_scheduled_run"] is False
    assert report["checked_at"] == NOW.isoformat()


def test_offline_runner_fails_even_with_recent_success():
    report = monitor.inspect_automation(now=NOW, api=api_for([run()], online=False))
    assert not report["healthy"]
    assert "offline" in report["errors"][0]


@pytest.mark.parametrize(
    "runs", [[], [run(age=49)], [run(branch="feature")], [run(event="pull_request")]]
)
def test_missing_stale_or_non_default_success_never_hides_failure(runs):
    report = monitor.inspect_automation(now=NOW, api=api_for(runs))
    assert not report["healthy"]
    assert report["catalog"]["missed_scheduled_run"]


def test_manual_success_cannot_mask_missing_scheduled_run():
    report = monitor.inspect_automation(
        now=NOW, api=api_for([run(event="workflow_dispatch", age=1)])
    )
    assert report["catalog"]["success_age_hours"] == 1
    assert report["catalog"]["missed_scheduled_run"]
    assert not report["healthy"]


def test_latest_cancelled_run_is_unhealthy_even_with_recent_success():
    report = monitor.inspect_automation(
        now=NOW, api=api_for([run(age=1, conclusion="cancelled"), run()])
    )
    assert not report["healthy"]
    assert "Latest completed" in report["errors"][0]


def test_daily_schedule_has_grace_period():
    before_deadline = NOW.replace(hour=7)
    yesterday = run(age=29)
    report = monitor.inspect_automation(now=before_deadline, api=api_for([yesterday]))
    assert report["healthy"]
    assert report["catalog"]["expected_schedule_at"].startswith("2026-11-12T06:17")


def test_running_job_after_deadline_does_not_count_as_success():
    report = monitor.inspect_automation(
        now=NOW, api=api_for([run(status="in_progress", conclusion=None), run(age=29)])
    )
    assert not report["healthy"]
    assert report["catalog"]["missed_scheduled_run"]


@pytest.mark.parametrize("kind", ["failure", "invalid", "timeout", "missing"])
def test_gh_failures_do_not_leak_raw_output(monkeypatch, kind):
    secret = "ghp-test-secret-do-not-log"

    def execute(*args, **kwargs):
        if kind == "timeout":
            raise subprocess.TimeoutExpired("gh", 30, output=secret)
        if kind == "missing":
            raise FileNotFoundError(secret)
        return SimpleNamespace(
            returncode=1 if kind == "failure" else 0, stdout=secret, stderr=secret
        )

    monkeypatch.setattr(monitor.subprocess, "run", execute)
    report = monitor.inspect_automation(now=NOW)
    assert not report["healthy"]
    assert secret not in json.dumps(report)


def test_api_failure_report_is_persisted_and_main_exits_nonzero(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        monitor,
        "inspect_automation",
        lambda *args, **kwargs: {"healthy": False, "errors": ["GitHub API unavailable"]},
    )
    output = tmp_path / "state" / "automation.json"
    assert monitor.main(["--output", str(output)]) == 1
    assert json.loads(output.read_text())["errors"] == ["GitHub API unavailable"]
    assert "unhealthy" in capsys.readouterr().out


def test_bad_api_shape_is_unhealthy():
    report = monitor.inspect_automation(now=NOW, api=lambda _: {})
    assert not report["healthy"]
    assert "Invalid" in report["errors"][0]


def test_rerun_completion_order_controls_latest_result():
    rerun = run(age=30, conclusion="failure")
    rerun["updated_at"] = (NOW - timedelta(minutes=5)).isoformat()
    report = monitor.inspect_automation(now=NOW, api=api_for([run(), rerun]))
    assert not report["healthy"]
    assert report["catalog"]["latest_completed"]["conclusion"] == "failure"
