"""Verified, isolated release installs with atomic activation and rollback.

Never mutate the environment serving an active session. Managed launchers read
one small manifest; changing it affects the next launch, not in-flight requests.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import config

_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_MAX_WHEEL_BYTES = 20 * 1024 * 1024
_CHECK_INTERVAL = 24 * 60 * 60
_check_thread: threading.Thread | None = None


@dataclass(frozen=True)
class Release:
    version: str
    filename: str
    url: str
    sha256: str


def runtime_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / "openvidia-managed"


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def installed_version() -> str:
    return importlib.metadata.version("openvidia")


def _version_tuple(version: str) -> tuple[int, ...]:
    if not _VERSION.fullmatch(version):
        raise ValueError("Only stable X.Y.Z releases are supported")
    return tuple(map(int, version.split(".")))


def fetch_release(version: str | None = None) -> Release:
    if version is not None:
        _version_tuple(version)
    suffix = f"/{version}" if version else ""
    response = httpx.get(f"https://pypi.org/pypi/openvidia{suffix}/json", timeout=15)
    response.raise_for_status()
    data = response.json()
    selected = data["info"]["version"]
    _version_tuple(selected)
    if version and selected != version:
        raise ValueError("Package index returned a different version")
    for item in data.get("urls", []):
        filename = item.get("filename", "")
        if (
            item.get("packagetype") == "bdist_wheel"
            and filename == f"openvidia-{selected}-py3-none-any.whl"
            and not item.get("yanked")
        ):
            url = item["url"]
            if (
                urlparse(url).scheme != "https"
                or urlparse(url).hostname != "files.pythonhosted.org"
            ):
                raise ValueError(
                    "Release wheel must be served by files.pythonhosted.org over HTTPS"
                )
            digest = item.get("digests", {}).get("sha256", "")
            if not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError("Release has no valid SHA-256 digest")
            return Release(selected, filename, url, digest)
    raise ValueError("Release has no supported, non-yanked universal wheel")


def check() -> dict:
    release = fetch_release()
    current = installed_version()
    result = {
        "checked_at": time.time(),
        "installed": current,
        "latest": release.version,
        "available": _version_tuple(release.version) > _version_tuple(current),
    }
    config.atomic_write(config.config_dir() / "update-check.json", json.dumps(result, indent=2))
    return result


def schedule_update_check() -> None:
    """Opt-in daily checks in a daemon thread; never install or delay startup."""
    global _check_thread
    settings = _read(config.config_dir() / "updates.json")
    if not settings.get("auto_check") or (_check_thread is not None and _check_thread.is_alive()):
        return

    def run() -> None:
        attempted_at = 0.0
        while _read(config.config_dir() / "updates.json").get("auto_check"):
            last = _read(config.config_dir() / "update-check.json")
            checked_at = max(last.get("checked_at", 0), attempted_at)
            if time.time() - checked_at >= _CHECK_INTERVAL:
                attempted_at = time.time()
                try:
                    result = check()
                    if result["available"]:
                        print(
                            f"OpenVidia {result['latest']} is available: openvidia update install",
                            flush=True,
                        )
                except Exception as exc:  # Network failures must not affect the proxy.
                    print(f"OpenVidia update check unavailable: {type(exc).__name__}", flush=True)
            # Re-read policy regularly while a server stays up for many days.
            time.sleep(60)

    _check_thread = threading.Thread(target=run, name="openvidia-update-check", daemon=True)
    _check_thread.start()


@contextmanager
def _install_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = root / ".update-lock"
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise RuntimeError(
            f"Another update holds {lock}; check it before removing a stale lock"
        ) from exc
    try:
        yield
    finally:
        lock.rmdir()


def _python(environment: Path) -> Path:
    return environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def _executable(environment: Path) -> Path:
    return environment / ("Scripts/openvidia.exe" if sys.platform == "win32" else "bin/openvidia")


def download_wheel(release: Release, destination: Path) -> None:
    digest = hashlib.sha256()
    size = 0
    with httpx.stream("GET", release.url, timeout=60, follow_redirects=False) as response:
        response.raise_for_status()
        with destination.open("xb") as output:
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > _MAX_WHEEL_BYTES:
                    raise ValueError("Release wheel exceeded the download size limit")
                digest.update(chunk)
                output.write(chunk)
    if digest.hexdigest() != release.sha256:
        destination.unlink(missing_ok=True)
        raise ValueError("Release wheel SHA-256 verification failed")


_SMOKE = """
import asyncio, importlib.metadata, pathlib, sys
import httpx, openvidia
from openvidia import config
config.config_dir = lambda path=pathlib.Path(sys.argv[2]): path
from openvidia.proxy_app import create_app
from openvidia.proxy_state import ProxyState, ProxyStats
assert importlib.metadata.version('openvidia') == sys.argv[1]
web = pathlib.Path(openvidia.__file__).parent / 'web'
assert all((web / x).is_file() for x in ['index.html', 'main.js', 'styles.css'])
async def run():
    app = create_app(ProxyState(keys=[], stats=ProxyStats(), log_cb=lambda m: None), web_dir=web)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost:1919') as c:
        response = await c.get('/health')
        assert response.status_code == 200, response.text
asyncio.run(run())
"""


def smoke_check(environment: Path, version: str) -> None:
    """Check installed imports, CLI and served health without touching real keys."""
    with tempfile.TemporaryDirectory(prefix="openvidia-smoke-") as directory:
        env = dict(os.environ)
        env["XDG_CONFIG_HOME"] = directory
        env["APPDATA"] = directory
        env.pop("OPENVIDIA_OUTBOUND_PROXY", None)
        env.pop("OPENVIDIA_REDIS_URL", None)
        subprocess.run(
            [str(_python(environment)), "-c", _SMOKE, version, directory],
            env=env,
            check=True,
            timeout=30,
            capture_output=True,
        )
        if not _executable(environment).is_file():
            raise ValueError("Installed release has no CLI entry point")
        # Invoke the console entry function with isolated config on every OS;
        # macOS deliberately ignores XDG_CONFIG_HOME and APPDATA in production.
        cli_smoke = (
            "import pathlib, sys; from openvidia import config; "
            "config.config_dir = lambda path=pathlib.Path(sys.argv[1]): path; "
            "from openvidia.__main__ import main; "
            "sys.argv = ['openvidia', '--help']; main()"
        )
        subprocess.run(
            [str(_python(environment)), "-c", cli_smoke, directory],
            env=env,
            check=True,
            timeout=15,
            capture_output=True,
        )


def _target(root: Path, entry: dict) -> Path:
    name = entry.get("environment", "")
    if not name or Path(name).name != name:
        raise ValueError("Invalid managed environment manifest")
    target = root / "releases" / name
    if not _executable(target).is_file():
        raise ValueError("Managed release is incomplete or missing")
    return target


def _write_launcher(root: Path) -> Path:
    # Self-contained: this launcher survives deletion of the source checkout or
    # of the Python environment that performed the update.
    script = root / "launch.py"
    launcher_code = """import json, os, pathlib, sys
root = pathlib.Path(__file__).resolve().parent
state = json.loads((root / "current.json").read_text())
name = state["current"]["environment"]
if not name or pathlib.Path(name).name != name:
    raise SystemExit("Invalid OpenVidia release manifest")
exe = root / "releases" / name / ("Scripts/openvidia.exe" if sys.platform == "win32" else "bin/openvidia")
os.execv(str(exe), [str(exe), *sys.argv[1:]])
"""
    config.atomic_write(script, launcher_code)
    python = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    if sys.platform == "win32":
        launcher = root / "openvidia-managed.cmd"
        config.atomic_write(launcher, f'@"{python}" "{script}" %*\n')
    else:
        import shlex

        launcher = root / "openvidia-managed"
        content = f'#!/bin/sh\nexec {shlex.quote(python)} {shlex.quote(str(script))} "$@"\n'
        config.atomic_write(launcher, content, mode=0o755)
    return launcher


def install(version: str | None = None, *, desktop: bool = False) -> dict:
    release = fetch_release(version)
    root = runtime_dir()
    with _install_lock(root):
        old = _read(root / "current.json")
        previous = old.get("current")
        if previous and _version_tuple(release.version) <= _version_tuple(previous["version"]):
            raise ValueError("Managed release is already current or newer; use rollback to go back")
        name = f"{release.version}-{uuid.uuid4().hex[:8]}"
        target = root / "releases" / name
        target.parent.mkdir(exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix="openvidia-wheel-") as directory:
                wheel = Path(directory) / release.filename
                download_wheel(release, wheel)
                subprocess.run([sys.executable, "-m", "venv", str(target)], check=True, timeout=120)
                requirement = str(wheel) + ("[desktop]" if desktop else "")
                subprocess.run(
                    [
                        str(_python(target)),
                        "-m",
                        "pip",
                        "install",
                        "--disable-pip-version-check",
                        requirement,
                    ],
                    check=True,
                    timeout=600,
                )
            smoke_check(target, release.version)
            launcher = _write_launcher(root)
            current = {"version": release.version, "environment": name, "desktop": desktop}
            state = {"current": current, "previous": previous, "activated_at": time.time()}
            # Only this atomic replace changes what the managed launcher runs.
            config.atomic_write(root / "current.json", json.dumps(state, indent=2))
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise
    return {**state, "launcher": str(launcher), "release": asdict(release)}


def rollback() -> dict:
    root = runtime_dir()
    with _install_lock(root):
        old = _read(root / "current.json")
        previous = old.get("previous")
        if not previous:
            raise ValueError("No previous managed release exists")
        target = _target(root, previous)
        smoke_check(target, previous["version"])
        state = {"current": previous, "previous": old.get("current"), "activated_at": time.time()}
        config.atomic_write(root / "current.json", json.dumps(state, indent=2))
        return state


def main(args: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openvidia update")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="Check the latest stable PyPI release")
    command = commands.add_parser("install", help="Verify and prepare an isolated managed release")
    command.add_argument("--version", help="Exact stable X.Y.Z release (latest by default)")
    command.add_argument(
        "--desktop", action="store_true", help="Include the native desktop dependencies"
    )
    commands.add_parser("rollback", help="Reactivate the previous verified managed release")
    commands.add_parser("status", help="Show managed release and last update check")
    command = commands.add_parser(
        "auto-check", help="Enable or disable daily update checks at startup"
    )
    command.add_argument("policy", choices=["on", "off"])
    options = parser.parse_args(args)
    try:
        if options.command == "check":
            result = check()
        elif options.command == "install":
            result = install(options.version, desktop=options.desktop)
        elif options.command == "rollback":
            result = rollback()
        elif options.command == "auto-check":
            result = {"auto_check": options.policy == "on"}
            config.atomic_write(config.config_dir() / "updates.json", json.dumps(result))
        else:
            result = {
                "managed": _read(runtime_dir() / "current.json"),
                "last_check": _read(config.config_dir() / "update-check.json"),
                "policy": _read(config.config_dir() / "updates.json"),
            }
        print(json.dumps(result, indent=2))
        if options.command in ("install", "rollback"):
            print("Activation affects the next managed launch. Existing sessions remain running.")
            print(
                f"Managed launcher: {runtime_dir() / ('openvidia-managed.cmd' if sys.platform == 'win32' else 'openvidia-managed')}"
            )
            print("Restart your managed service during a maintenance window to use this release.")
        return 0
    except (OSError, ValueError, RuntimeError, httpx.HTTPError, subprocess.SubprocessError) as exc:
        print(f"Update failed; the active managed release was not changed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
