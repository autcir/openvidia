"""Desktop attachment, service restart and readiness regressions."""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from openvidia import __main__ as cli
from openvidia.proxy_app import create_app
from openvidia.proxy_state import ProxyState, ProxyStats
from openvidia.server_manager import ProxyServer


def test_desktop_reuses_service_without_spawning_or_killing(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["openvidia"])
    monkeypatch.setattr(cli, "_proxy_is_serving", lambda _port: True)
    spawn = Mock(side_effect=AssertionError("must reuse running service"))
    monkeypatch.setattr("subprocess.Popen", spawn)
    desk = Mock()
    monkeypatch.setattr(cli, "open_desk", desk)
    cli.main()
    desk.assert_called_once_with(cli.PORT)
    spawn.assert_not_called()


def test_desktop_refuses_unrelated_listener_without_spawning(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["openvidia"])
    monkeypatch.setattr(cli, "_proxy_is_serving", lambda _port: False)
    monkeypatch.setattr(cli, "_port_is_open", lambda _port: True)
    spawn = Mock(side_effect=AssertionError("must leave listener untouched"))
    monkeypatch.setattr("subprocess.Popen", spawn)
    with pytest.raises(SystemExit, match="1"):
        cli.main()
    spawn.assert_not_called()


def test_health_probe_does_not_accept_an_unrelated_http_service(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"status":"ok","port":1919}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: Response())
    assert cli._proxy_is_serving(1919) is False


def test_health_probe_checks_openvidia_identity_and_port(monkeypatch):
    import io

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **kw: io.BytesIO(json.dumps({"service": "openvidia", "port": 1919}).encode()),
    )
    assert cli._proxy_is_serving(1919) is True
    assert cli._proxy_is_serving(9999) is False


def test_window_without_tray_closes_without_stopping_proxy(monkeypatch):
    events = SimpleNamespace(closing=[], closed=[])
    window = SimpleNamespace(events=events)
    webview = SimpleNamespace(create_window=Mock(return_value=window), start=Mock())
    monkeypatch.setitem(sys.modules, "webview", webview)
    monkeypatch.setattr(cli, "_make_signaller", lambda *args: None)
    cli.open_desk(1919)
    assert events.closed == []
    assert events.closing == []
    webview.start.assert_called_once()


@pytest.mark.asyncio
async def test_foreground_refuses_occupied_port_without_terminating_it(monkeypatch):
    monkeypatch.setattr(cli, "_port_is_open", lambda _port: True)
    with pytest.raises(SystemExit, match="left untouched"):
        await cli.main_async()


@pytest.mark.asyncio
async def test_restart_waits_for_shutdown_then_replaces_same_process(monkeypatch):
    order = []
    event = asyncio.Event()
    event.set()
    task = asyncio.create_task(asyncio.Event().wait())

    async def shutdown():
        order.append("drained")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    server = SimpleNamespace(
        state=SimpleNamespace(log_cb=lambda _m: None),
        tasks=[task],
        restart_requested=event,
        shutdown=shutdown,
    )

    async def start(*args, **kwargs):
        return server

    monkeypatch.setattr(cli, "_port_is_open", lambda _port: False)
    monkeypatch.setattr(cli, "start", start)
    monkeypatch.setattr(cli.config, "harden_config_permissions", lambda: [])
    monkeypatch.setattr(cli.config, "load_saved_keys_file", lambda: ["test-key"])
    monkeypatch.setattr(cli.config, "load_active_model", lambda: "test/model")
    monkeypatch.setattr(cli.os, "execv", lambda executable, args: order.append((executable, args)))
    await cli.main_async()
    assert order == ["drained", (sys.executable, [sys.executable, "-m", "openvidia", "foreground"])]


@pytest.mark.asyncio
async def test_shutdown_drains_server_tasks():
    native = SimpleNamespace(should_exit=False)

    async def serve():
        while not native.should_exit:
            await asyncio.sleep(0)

    task = asyncio.create_task(serve())
    proxy = ProxyServer([native], [task], SimpleNamespace())
    await proxy.shutdown()
    assert task.done()
    assert not task.cancelled()


@pytest.mark.asyncio
async def test_readiness_tracks_pause_and_pool_exhaustion_without_ui():
    state = ProxyState(["key"], ProxyStats(), lambda _m: None)
    app = create_app(state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:1919"
    ) as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/ready")).status_code == 200
        state.running = False
        assert (await client.get("/ready")).status_code == 503
        assert (await client.get("/health")).status_code == 200
        state.running = True
        state.mark_key_failed("key", status=429)
        assert (await client.get("/ready")).status_code == 503


@pytest.mark.asyncio
async def test_restart_requires_owner_and_schedules_drain_after_response():
    from pathlib import Path

    from openvidia import config

    state = ProxyState([], ProxyStats(), lambda _m: None)
    app = create_app(state, web_dir=Path(cli.__file__).parent / "web")
    called = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost:1919",
        headers={"x-openvidia-token": config.control_token()},
    ) as client:
        response = await client.post("/api/restart")
        assert response.status_code == 409
        app.state.request_restart = lambda: called.append("restart")
        response = await client.post("/api/restart")
        assert response.status_code == 200
        assert called == []
        await asyncio.sleep(0.25)
        assert called == ["restart"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX exec preserves the process ID")
def test_real_subprocess_restart_preserves_pid_and_desktop_reuses_it(tmp_path):
    """Exercise actual uvicorn sockets, HTTP restart and exec in isolation."""
    import os
    import socket
    import subprocess
    import time
    from pathlib import Path

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    configuration = tmp_path / "config" / "openvidia"
    configuration.mkdir(parents=True)
    (configuration / "keys.json").write_text('["nvapi-test-not-a-real-key"]')
    starts = tmp_path / "starts"
    harness = tmp_path / "service.py"
    harness.write_text(
        "import asyncio, os, sys\n"
        "from pathlib import Path\n"
        "from openvidia import __main__ as cli, proxy_app, config\n"
        f"config.config_dir = lambda: Path({str(configuration)!r})\n"
        "async def no_network(*args, **kwargs): pass\n"
        "proxy_app._health_check_all = no_network\n"
        "proxy_app._background_health_check = no_network\n"
        "proxy_app._background_upstream_probe = no_network\n"
        f"with Path({str(starts)!r}).open('a') as log: log.write(str(os.getpid())+'\\n')\n"
        "original_exec = os.execv\n"
        f"cli.os.execv = lambda executable, args: original_exec(sys.executable, [sys.executable, {str(harness)!r}])\n"
        "asyncio.run(cli.main_async())\n"
    )
    environment = {
        **os.environ,
        "OPENVIDIA_PORT": str(port),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "APPDATA": str(tmp_path / "config"),
        "PYTHONPATH": str(Path(cli.__file__).parent.parent),
    }
    with (tmp_path / "service.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, str(harness)], env=environment, stdout=log, stderr=log
        )
        try:
            base = f"http://127.0.0.1:{port}"
            with httpx.Client(base_url=base, timeout=0.5, trust_env=False) as client:
                deadline = time.monotonic() + 15
                while True:
                    try:
                        if client.get("/health").json().get("service") == "openvidia":
                            break
                    except (httpx.HTTPError, ValueError):
                        pass
                    assert process.poll() is None, (tmp_path / "service.log").read_text()
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
                token = (configuration / "control_token").read_text().strip()
                response = client.post("/api/restart", headers={"x-openvidia-token": token})
                assert response.status_code == 200
                deadline = time.monotonic() + 15
                while len(starts.read_text().splitlines()) < 2:
                    assert process.poll() is None, (tmp_path / "service.log").read_text()
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
                while True:
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
                assert starts.read_text().splitlines() == [str(process.pid), str(process.pid)]
                reused = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "from openvidia import __main__ as m; m.open_desk=lambda port: print('ATTACHED'); m.main()",
                    ],
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                assert reused.returncode == 0, reused.stderr
                assert "ATTACHED" in reused.stdout
                assert process.poll() is None
                assert len(starts.read_text().splitlines()) == 2
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
