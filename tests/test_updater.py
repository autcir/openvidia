"""Managed updates must never damage the release currently serving requests."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from openvidia import updater as u


@pytest.fixture
def managed(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    settings = tmp_path / "config"
    settings.mkdir()
    monkeypatch.setattr(u, "runtime_dir", lambda: root)
    monkeypatch.setattr(u.config, "config_dir", lambda: settings)
    return root, settings


def release(version="2.3.0", content=b"verified wheel"):
    return u.Release(
        version,
        f"openvidia-{version}-py3-none-any.whl",
        "https://files.pythonhosted.org/packages/openvidia.whl",
        hashlib.sha256(content).hexdigest(),
    )


def add_release(root, version, name):
    target = root / "releases" / name
    binary = u._executable(target)
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nprintf '%s' '" + version + "'\n")
    binary.chmod(0o755)
    return {"version": version, "environment": name, "desktop": False}


def set_state(root, current, previous=None):
    u.config.atomic_write(
        root / "current.json", json.dumps({"current": current, "previous": previous})
    )


def fake_install(monkeypatch):
    def run(args, **kwargs):
        if args[1:3] == ["-m", "venv"]:
            binary = u._executable(Path(args[3]))
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_text("candidate")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(u.subprocess, "run", run)
    monkeypatch.setattr(u, "fetch_release", lambda version=None: release(version or "2.3.0"))
    monkeypatch.setattr(u, "download_wheel", lambda rel, dest: dest.write_bytes(b"verified wheel"))


def test_failed_candidate_does_not_activate_or_delete_current(managed, monkeypatch):
    root, _ = managed
    current = add_release(root, "2.2.0", "2.2.0-existing")
    set_state(root, current)
    before = (root / "current.json").read_bytes()
    fake_install(monkeypatch)

    def fail(*args):
        raise subprocess.CalledProcessError(1, ["candidate-health"])

    monkeypatch.setattr(u, "smoke_check", fail)
    with pytest.raises(subprocess.CalledProcessError):
        u.install()
    assert (root / "current.json").read_bytes() == before
    assert u._executable(root / "releases" / current["environment"]).exists()
    assert list((root / "releases").iterdir()) == [root / "releases" / current["environment"]]
    assert not (root / ".update-lock").exists()


def test_success_activates_only_after_smoke_and_keeps_rollback(managed, monkeypatch):
    root, _ = managed
    current = add_release(root, "2.2.0", "2.2.0-existing")
    set_state(root, current)
    fake_install(monkeypatch)
    observed = []

    def smoke(target, version):
        observed.append(version)
        # Candidate code runs while the manifest still selects the old release.
        assert json.loads((root / "current.json").read_text())["current"] == current
        assert u._executable(target).exists()

    monkeypatch.setattr(u, "smoke_check", smoke)
    state = u.install(desktop=True)
    assert observed == ["2.3.0"]
    assert state["current"]["version"] == "2.3.0"
    assert state["current"]["desktop"] is True
    assert state["previous"] == current
    assert Path(state["launcher"]).is_file()
    assert u._executable(root / "releases" / current["environment"]).exists()


def test_rollback_checks_previous_and_changes_next_launcher(managed, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("Shell launcher execution is POSIX-specific")
    root, _ = managed
    old = add_release(root, "2.2.0", "old")
    new = add_release(root, "2.3.0", "new")
    set_state(root, new, old)
    launcher = u._write_launcher(root)
    assert subprocess.check_output([str(launcher)], text=True) == "2.3.0"
    checked = []
    monkeypatch.setattr(u, "smoke_check", lambda target, version: checked.append(version))
    state = u.rollback()
    assert checked == ["2.2.0"]
    assert state["previous"] == new
    assert subprocess.check_output([str(launcher)], text=True) == "2.2.0"


def test_broken_rollback_leaves_manifest_intact(managed, monkeypatch):
    root, _ = managed
    old = add_release(root, "2.2.0", "old")
    new = add_release(root, "2.3.0", "new")
    set_state(root, new, old)
    u._executable(root / "releases" / "old").unlink()
    before = (root / "current.json").read_bytes()
    with pytest.raises(ValueError, match="incomplete"):
        u.rollback()
    assert (root / "current.json").read_bytes() == before


def test_second_updater_cannot_change_manifest(managed):
    root, _ = managed
    with u._install_lock(root), pytest.raises(RuntimeError, match="Another update"):
        u.rollback()


def test_digest_mismatch_rejects_wheel(tmp_path, monkeypatch):
    client = httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b"tampered"))
    )
    monkeypatch.setattr(u.httpx, "stream", client.stream)
    dest = tmp_path / "wheel.whl"
    with pytest.raises(ValueError, match="SHA-256"):
        u.download_wheel(release(), dest)
    assert not dest.exists()
    client.close()


def test_pypi_yanked_and_foreign_wheels_are_rejected(monkeypatch):
    rel = release()
    item = {
        "packagetype": "bdist_wheel",
        "filename": rel.filename,
        "url": rel.url,
        "digests": {"sha256": rel.sha256},
        "yanked": True,
    }
    data = {"info": {"version": rel.version}, "urls": [item]}
    monkeypatch.setattr(
        u.httpx,
        "get",
        lambda *a, **kw: httpx.Response(
            200, json=data, request=httpx.Request("GET", "https://pypi.org")
        ),
    )
    with pytest.raises(ValueError, match="non-yanked"):
        u.fetch_release()
    item["yanked"] = False
    item["url"] = "https://attacker.example/wheel.whl"
    with pytest.raises(ValueError, match="pythonhosted"):
        u.fetch_release()


def test_update_checks_opt_in_and_fail_without_affecting_startup(managed, monkeypatch):
    _, settings = managed
    calls = []
    monkeypatch.setattr(u, "check", lambda: calls.append("checked"))
    u.schedule_update_check()
    assert calls == []
    assert u.main(["auto-check", "on"]) == 0
    assert json.loads((settings / "updates.json").read_text()) == {"auto_check": True}


def test_no_downgrade_through_install(managed, monkeypatch):
    root, _ = managed
    set_state(root, add_release(root, "2.3.0", "current"))
    monkeypatch.setattr(u, "fetch_release", lambda version=None: release("2.2.0"))
    with pytest.raises(ValueError, match="rollback"):
        u.install("2.2.0")


def test_candidate_checks_preserve_home_and_isolate_config_on_macos(managed, monkeypatch):
    root, _ = managed
    target = root / "candidate"
    executable = target / "bin/openvidia"
    executable.parent.mkdir(parents=True)
    executable.write_text("candidate")
    calls = []
    monkeypatch.setattr(u.sys, "platform", "darwin")
    monkeypatch.setattr(
        u.subprocess, "run", lambda command, **kwargs: calls.append((command, kwargs))
    )
    u.smoke_check(target, "2.3.0")
    assert len(calls) == 2
    import os

    for command, kwargs in calls:
        assert kwargs["env"].get("HOME") == os.environ.get("HOME")
        assert "config.config_dir = lambda" in command[2]
    assert calls[0][0][-2] == "2.3.0"
