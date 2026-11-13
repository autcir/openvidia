"""Run installer paths in a sandbox; never invoke package managers or services."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="Bash source installer is POSIX-only"
)


@pytest.fixture
def installer(tmp_path):
    repo = tmp_path / "source tree"
    repo.mkdir()
    shutil.copy2(REPO / "install.sh", repo / "install.sh")
    shutil.copy2(REPO / "openvidia.desktop", repo / "openvidia.desktop")
    assets = repo / "openvidia/web/assets"
    assets.mkdir(parents=True)
    (assets / "logo.png").write_bytes(b"icon")
    binary = repo / ".venv/bin/openvidia"
    binary.parent.mkdir(parents=True)
    binary.write_text('#!/bin/sh\nprintf "openvidia %s\\n" "$*" >> "$INSTALL_TRACE"\n')
    binary.chmod(0o755)
    commands = tmp_path / "commands"
    commands.mkdir()
    for name in ("uv", "systemctl", "update-desktop-database", "desktop-file-validate"):
        command = commands / name
        command.write_text(f'#!/bin/sh\nprintf "{name} %s\\n" "$*" >> "$INSTALL_TRACE"\n')
        command.chmod(0o755)
    home = tmp_path / "user"
    home.mkdir()
    trace = tmp_path / "trace"
    env = {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "PATH": str(commands) + os.pathsep + os.environ["PATH"],
        "INSTALL_TRACE": str(trace),
    }
    return repo, home, trace, env


def test_headless_installer_skips_gui_and_desktop_registration(installer):
    repo, home, trace, env = installer
    subprocess.run(
        ["bash", str(repo / "install.sh"), "--headless", "--no-start"],
        env=env,
        check=True,
        capture_output=True,
    )
    calls = trace.read_text()
    assert "uv sync --frozen --no-dev --quiet\n" in calls
    assert "--extra desktop" not in calls
    assert "openvidia setup" in calls
    assert not (home / ".local/share/applications/openvidia.desktop").exists()
    assert not (home / ".config/systemd/user/openvidia.service").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux desktop registration")
def test_desktop_installer_requests_extra_and_registers_absolute_launcher(installer):
    repo, home, trace, env = installer
    subprocess.run(
        ["bash", str(repo / "install.sh"), "--desktop", "--no-start"],
        env=env,
        check=True,
        capture_output=True,
    )
    assert "--extra desktop" in trace.read_text()
    desktop = (home / ".local/share/applications/openvidia.desktop").read_text()
    launcher = home / ".local/bin/openvidia-desktop"
    assert f"TryExec={launcher}" in desktop
    assert f'Exec="{launcher}"' in desktop
    assert "/usr/bin/openvidia" not in desktop
    assert launcher.is_file()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux systemd user service")
def test_service_registration_is_explicit_and_no_start_is_respected(installer):
    repo, home, trace, env = installer
    subprocess.run(
        ["bash", str(repo / "install.sh"), "--headless", "--service", "--no-start"],
        env=env,
        check=True,
        capture_output=True,
    )
    unit = home / ".config/systemd/user/openvidia.service"
    assert unit.exists()
    assert f'ExecStart="{repo}/.venv/bin/openvidia" foreground' in unit.read_text()
    assert "Restart=always" in unit.read_text()
    calls = trace.read_text()
    assert "systemctl --user enable openvidia.service" in calls
    assert "--now" not in calls
    assert "foreground" not in calls


def test_base_distribution_has_no_graphical_dependencies():
    project = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
    assert not any("qt" in x.lower() or "webview" in x.lower() for x in project["dependencies"])
    desktop = project["optional-dependencies"]["desktop"]
    assert any(x.startswith("pywebview") for x in desktop)
    assert any(x.startswith("PyQt6-WebEngine") for x in desktop)
