"""
OpenVidia — minimal multi-key NVIDIA API proxy with desktop app.

Install:
    pip install -e .

Usage:
    openvidia                    # start proxy + desktop window
    openvidia foreground         # foreground mode (logs stdout)
    openvidia setup              # configure opencode + Jcode (the persistent edits)
    openvidia run <cli> [args]   # run any CLI against the proxy, writing nothing

Dashboard + API at http://localhost:1919
Edit keys via ~/.config/openvidia/keys.json or dashboard Keys tab.
"""

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

from . import config
from .proxy_state import ProxyStats
from .server_manager import start

# ---------------------------------------------------------------------------
# Configuration — entrypoint constants
# ---------------------------------------------------------------------------
PORT = int(os.environ.get("OPENVIDIA_PORT", "1919"))
if not 1 <= PORT <= 65535:
    raise ValueError("OPENVIDIA_PORT must be between 1 and 65535")
ENV_VAR = "OPENVIDIA_API_KEY"
ENV_VAL = "ignored"
_tray_ref = None  # Global tray reference (anti-GC)
_tray_hide = None  # Global hide-function reference for close-to-tray


def _proxy_is_serving(port: int) -> bool:
    """Identify OpenVidia by its HTTP health response, never by an open port."""
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.0) as response:
            data = json.load(response)
        return data.get("service") == "openvidia" and data.get("port") == port
    except (OSError, ValueError, AttributeError):
        return False


def _port_is_open(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _setup_opencode():
    """Configure the opencode CLI (~/.config/opencode/opencode.json) to use OpenVidia."""
    oc_path = config.opencode_config_path()
    if not oc_path.exists():
        print(f"ℹ opencode not found at {oc_path} — skipping")
        return False
    try:
        cfg = json.loads(oc_path.read_text())
    except (json.JSONDecodeError, OSError):
        print(f"✗ Invalid opencode config at {oc_path}")
        return False

    changed = False
    providers = cfg.setdefault("provider", {})

    # Remove the orphan nvidia provider if it points to localhost
    nv = providers.get("nvidia", {})
    if isinstance(nv, dict) and nv.get("options", {}).get("baseURL", "").startswith(
        "http://localhost"
    ):
        del providers["nvidia"]
        changed = True

    if "openvidia" not in providers:
        providers["openvidia"] = {
            "models": {"openvidia": {"name": "OpenVidia", "tools": True}},
            "npm": "@ai-sdk/openai-compatible",
            "options": {
                "apiKey": "ignored",
                "baseURL": f"http://localhost:{PORT}/v1",
            },
        }
        changed = True
        print("✓ Added OpenVidia provider to opencode")
    else:
        ov = providers["openvidia"]
        m = ov.setdefault("models", {})
        if "openvidia" not in m:
            m["openvidia"] = {"name": "OpenVidia", "tools": True}
            changed = True
            print("✓ Added OpenVidia model to opencode provider")

    # Auto-compaction, but only as a default. Forcing it back on every run
    # overrode users who had deliberately turned it off.
    if "compaction" not in cfg:
        cfg["compaction"] = {"auto": True, "prune": True, "reserved": 8000}
        changed = True
        print("✓ Enabled auto-compaction (prune=true, reserved=8000)")

    # Select the provider only when nothing is selected. This used to be
    # unconditional and ran on every proxy start, so a user who picked another
    # provider in opencode found it reset the next time OpenVidia launched.
    if not cfg.get("model"):
        cfg["model"] = "openvidia/openvidia"
        changed = True
        print("✓ Default model set to openvidia/openvidia")
    elif cfg.get("model") != "openvidia/openvidia":
        print(f"ℹ Leaving your opencode model as {cfg['model']} — switch with /model openvidia")

    if not cfg.get("small_model"):
        cfg["small_model"] = "openvidia/openvidia"
        changed = True
        print("✓ Small model set to openvidia/openvidia")

    # NOTE: the previous version also prepended Path.cwd()/AGENTS.md to
    # opencode's GLOBAL instructions. Launched from this repo — which the
    # desktop launcher does — that meant every opencode project everywhere
    # started loading OpenVidia's own AGENTS.md. A proxy has no business
    # deciding what instructions another tool reads.

    if changed:
        # Someone else's config file: back it up before touching it. This is
        # the protection save_keys_file already gave OpenVidia's own data.
        try:
            from .safe_file import create_backup

            create_backup(oc_path)
        except OSError as e:
            print(f"✗ could not back up {oc_path}: {e} — not writing")
            return False
        config.atomic_write(oc_path, json.dumps(cfg, indent=2), mode=0o644)

    print(f"✓ OpenVidia provider ready → http://localhost:{PORT}/v1")
    print(f"✓ Dashboard at http://localhost:{PORT}")
    return True


def _setup_jcode():
    """Configure Jcode (~/.jcode/config.toml) to use OpenVidia as a provider.

    Jcode is an open-source coding agent (https://github.com/1jehuang/jcode)
    that supports OpenAI-compatible providers via TOML config. This adds the
    ``openvidia`` provider block and sets it as default — but only when no
    provider is configured yet, so a user who picked another provider keeps it.

    Uses tomlkit to preserve existing comments and formatting.
    """
    jc_path = config.jcode_config_path()
    if not jc_path.parent.exists():
        print(f"ℹ Jcode not found ({jc_path.parent} does not exist) — skipping")
        return False

    try:
        import tomlkit
    except ImportError:
        print("ℹ tomlkit not installed — skipping Jcode setup")
        return False

    # Read existing config (or start fresh)
    if jc_path.exists():
        try:
            from .safe_file import create_backup

            create_backup(jc_path)
        except OSError as e:
            print(f"✗ could not back up {jc_path}: {e} — not writing")
            return False
        try:
            doc = tomlkit.parse(jc_path.read_text())
        except Exception:
            print(f"✗ Invalid Jcode config at {jc_path} — skipping")
            return False
    else:
        doc = tomlkit.document()

    changed = False

    # ── Provider block: [providers.openvidia] ──────────────────────────
    providers = doc.get("providers", None)
    if providers is None:
        providers = tomlkit.table()
        doc["providers"] = providers
        changed = True

    if "openvidia" not in providers:
        ov = tomlkit.table()
        ov["type"] = "openai-compatible"
        ov["base_url"] = BASE_URL
        ov["auth"] = "none"
        ov["default_model"] = "openvidia"
        ov["requires_api_key"] = False
        ov["model_catalog"] = True
        ov["stream_idle_timeout_secs"] = 300

        # Model entries
        models = tomlkit.aot()
        m1 = tomlkit.table()
        m1["id"] = "openvidia"
        m1["context_window"] = 202752
        models.append(m1)
        ov["models"] = models

        providers["openvidia"] = ov
        changed = True
        print("✓ Added OpenVidia provider to Jcode")
    else:
        # Ensure base_url is current (port could have changed)
        ov = providers["openvidia"]
        if ov.get("base_url") != BASE_URL:
            ov["base_url"] = BASE_URL
            changed = True
            print("✓ Updated OpenVidia base_url in Jcode")

    # ── Default provider: only when unset ──────────────────────────────
    provider_section = doc.get("provider", None)
    if provider_section is None:
        provider_section = tomlkit.table()
        doc["provider"] = provider_section
        changed = True

    current_default = provider_section.get("default_provider")
    if not current_default:
        provider_section["default_provider"] = "openvidia"
        changed = True
        print("✓ Default provider set to openvidia")
    elif current_default != "openvidia":
        print(f"ℹ Leaving your Jcode default provider as {current_default}")

    current_model = provider_section.get("default_model")
    if not current_model:
        provider_section["default_model"] = "openvidia"
        changed = True
        print("✓ Default model set to openvidia")

    if changed:
        config.atomic_write(jc_path, tomlkit.dumps(doc), mode=0o644)

    print(f"✓ OpenVidia provider ready in Jcode → {jc_path}")
    return True


def _setup_proxy_config():
    """Ensure ~/.config/openvidia/proxy_config.json exists with default settings."""
    cfg_dir = config.config_dir()
    p = cfg_dir / "proxy_config.json"
    if not p.exists():
        default_cfg = {
            "outbound_proxy": "",
            "comment": "Set outbound_proxy (e.g. http://user:pass@proxy.example.com:8080) for IP rotation across large key pools",
        }
        config.atomic_write(p, json.dumps(default_cfg, indent=2))
        print(f"✓ Created proxy config template → {p}")
    else:
        print(f"✓ Proxy config ready → {p}")


# ---------------------------------------------------------------------------
# Running another CLI against the proxy
# ---------------------------------------------------------------------------
# The proxy used to reconfigure opencode, Codex, Claude Code and Grok on every
# single start — not just on `setup`. That reset the user's chosen opencode
# model to openvidia/openvidia on each launch, re-enabled compaction settings
# they had turned off, rewrote ~/.codex/config.toml and ~/.grok/config.toml
# without a backup, appended to shell rc files (with bash syntax, even under
# fish, so every new shell printed an error), and injected whatever AGENTS.md
# happened to be in the launch directory into opencode's GLOBAL instructions.
#
# `openvidia run <cli>` replaces all of that with the ollama model: nothing on
# disk changes, ever. The environment is set for one child process and the CLI
# is exec'd into it.
BASE_URL = f"http://localhost:{PORT}/v1"
ROOT_URL = f"http://localhost:{PORT}"

# Defaults ship for the variables these tools document. The table is
# user-editable at ~/.config/openvidia/cli_targets.json so a CLI that renames
# its variables tomorrow is a config edit rather than a release — the same
# choice model_options.json already makes for payload flags.
_DEFAULT_CLI_TARGETS: dict[str, dict] = {
    "opencode": {
        "argv": ["opencode"],
        "env": {"OPENAI_BASE_URL": BASE_URL, "OPENAI_API_KEY": ENV_VAL, ENV_VAR: ENV_VAL},
    },
    "claude": {
        "argv": ["claude"],
        "env": {"ANTHROPIC_BASE_URL": ROOT_URL, "ANTHROPIC_API_KEY": ENV_VAL},
    },
    "codex": {
        "argv": ["codex"],
        "env": {"OPENAI_BASE_URL": BASE_URL, "OPENAI_API_KEY": ENV_VAL, ENV_VAR: ENV_VAL},
        # Codex resolves providers from ~/.codex/config.toml, so the base URL
        # alone may not be enough. We refuse to write that file behind the
        # user's back; print it instead and let them decide.
        "note": (
            "Codex may also need a provider block in ~/.codex/config.toml.\n"
            "  If it does not pick up the proxy, add once:\n"
            "\n"
            '    model = "openvidia"\n'
            '    model_provider = "openvidia"\n'
            "\n"
            "    [model_providers.openvidia]\n"
            '    name = "OpenVidia"\n'
            f'    base_url = "{BASE_URL}"\n'
            f'    env_key = "{ENV_VAR}"\n'
            '    wire_api = "responses"'
        ),
    },
    "grok": {
        "argv": ["grok"],
        "env": {"OPENAI_BASE_URL": BASE_URL, "OPENAI_API_KEY": ENV_VAL, ENV_VAR: ENV_VAL},
    },
    "jcode": {
        "argv": ["jcode"],
        "env": {"OPENAI_BASE_URL": BASE_URL, "OPENAI_API_KEY": ENV_VAL, ENV_VAR: ENV_VAL},
    },
}


def _cli_targets() -> dict[str, dict]:
    """Ship defaults, let the user override them without a release."""
    targets = {k: dict(v) for k, v in _DEFAULT_CLI_TARGETS.items()}
    try:
        p = config.config_dir() / "cli_targets.json"
        if p.exists():
            user = json.loads(p.read_text())
            if isinstance(user, dict):
                for name, spec in user.items():
                    if isinstance(spec, dict):
                        targets[str(name)] = {**targets.get(str(name), {}), **spec}
    except (json.JSONDecodeError, OSError):
        pass
    return targets


def _run_cmd(argv: list[str]) -> int:
    """``openvidia run <cli> [args...]`` — point one process at the proxy.

    Nothing is written. The variables live in this process's child and die
    with it, so a CLI configured against the proxy today is unmodified
    tomorrow, and the user's own config keeps whatever they put in it.
    """
    targets = _cli_targets()
    if not argv:
        print("Usage: openvidia run <cli> [args...]")
        print(f"Known: {', '.join(sorted(targets))}")
        print("Anything else is executed as-is with the OpenAI-compatible vars set.")
        return 2

    name, rest = argv[0], argv[1:]
    spec = targets.get(name)
    if spec is None:
        # Not in the table: still useful — most tools read OPENAI_BASE_URL.
        spec = {
            "argv": [name],
            "env": {"OPENAI_BASE_URL": BASE_URL, "OPENAI_API_KEY": ENV_VAL, ENV_VAR: ENV_VAL},
        }

    env = {**os.environ, **{str(k): str(v) for k, v in (spec.get("env") or {}).items()}}
    cmd = list(spec.get("argv") or [name]) + rest

    exe = shutil.which(cmd[0])
    if exe is None:
        print(f"✗ {cmd[0]} is not on PATH")
        return 127

    if not _proxy_is_up(PORT):
        print(f"⚠ Nothing is serving {ROOT_URL} — start it with `openvidia` first.")

    for k in sorted(spec.get("env") or {}):
        print(f"  {k}={spec['env'][k]}")
    if spec.get("note"):
        print(f"\nℹ {spec['note']}\n")
    print(f"→ exec {' '.join(cmd)}\n", flush=True)

    try:
        os.execvpe(exe, cmd, env)
    except OSError as e:  # pragma: no cover — execvpe only returns on failure
        print(f"✗ could not exec {cmd[0]}: {e}")
        return 126


def _proxy_is_up(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _setup_cmd():
    """``openvidia setup`` — configure opencode and Jcode.

    opencode and Jcode are the two CLIs this project treats as first-class,
    so they get a persistent config edit on setup. Everything else runs
    through `openvidia run`, which writes nothing.
    """
    print("OpenVidia setup")
    print()
    _setup_proxy_config()
    print()
    _setup_opencode()
    print()
    _setup_jcode()
    print()
    print(f"Proxy:      {BASE_URL}")
    print(f"Dashboard:  {ROOT_URL}")
    print()
    print("Other CLIs need no setup — run them against the proxy on demand:")
    for name in sorted(n for n in _cli_targets() if n not in ("opencode", "jcode")):
        print(f"  openvidia run {name}")
    sys.exit(0)


async def main_async():
    """Foreground entrypoint: start the proxy on the event loop."""
    if _port_is_open(PORT):
        raise SystemExit(f"Port {PORT} is already in use; the existing process was left untouched")
    # Deliberately no _setup_*() here. Starting a proxy is not consent to
    # rewrite four other tools' configuration files.
    repaired = config.harden_config_permissions()
    if repaired:
        print(f"  ✓ tightened permissions on {', '.join(repaired)} (was world-readable)")
    keys = config.load_saved_keys_file()
    if not keys:
        print("✗ No keys found. Add keys to ~/.config/openvidia/keys.json")
        print("  Or run: openvidia setup")
        sys.exit(1)

    stats = ProxyStats()
    saved_model = config.load_active_model()

    def log(msg: str):
        print(msg, flush=True)

    web_dir = Path(__file__).resolve().parent / "web"
    srv = await start(
        PORT,
        keys,
        log,
        stats,
        web_dir=web_dir,
        initial_model=saved_model,
    )
    srv.state.log_cb(f"● OpenVidia running on :{PORT} ({len(keys)} keys)")
    from .updater import schedule_update_check

    schedule_update_check()

    # A restart replaces this process after its sockets and streams drain.
    # Keeping the PID avoids a second instance racing systemd's supervisor.
    restart_wait = asyncio.create_task(srv.restart_requested.wait())
    restart = False
    try:
        await asyncio.wait([restart_wait, *srv.tasks], return_when=asyncio.FIRST_COMPLETED)
        restart = restart_wait.done() and restart_wait.result()
    finally:
        restart_wait.cancel()
        await asyncio.gather(restart_wait, return_exceptions=True)
        await srv.shutdown()
    if restart:
        os.execv(sys.executable, [sys.executable, "-m", "openvidia", "foreground"])


def _wait_until_serving(port: int, child=None, timeout: float = 20.0) -> bool:
    """Wait for OpenVidia HTTP health, not merely a listening socket."""
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if _proxy_is_serving(port):
            return True
        if child is not None and child.poll() is not None:
            print(f"Proxy exited during startup (code {child.returncode})", flush=True)
            return False
        _time.sleep(0.2)
    print(f"OpenVidia did not become healthy on :{port} within {timeout:.0f}s", flush=True)
    return False


def _make_signaller(icon_path, window, port):
    """Create a QObject in the main thread with a 'create' signal.

    The cross-thread emit is dispatched onto the Qt main loop via
    QueuedConnection, so _create_tray runs in the right thread.
    """
    try:
        from PyQt6.QtCore import QObject, pyqtSignal
    except ImportError:
        return None

    class _Sig(QObject):
        create = pyqtSignal()

    s = _Sig()
    s.create.connect(lambda: _create_tray(icon_path, window, port))
    return s


def _tray_waiter_factory(signaller, window):
    """Build the function passed to webview.start(func=...).

    1) Wait for the QCoreApplication (pywebview creates it)
    2) Wait for the window shown event
    3) Emit the cross-thread signal -> tray on the Qt main loop
    """
    import time as _time

    def _waiter():
        from PyQt6.QtCore import QCoreApplication

        for _ in range(100):
            if QCoreApplication.instance() is not None:
                break
            _time.sleep(0.2)
        else:
            return

        if not window.events.shown.wait(20):
            return

        signaller.create.emit()

    return _waiter


def _create_tray(icon_path: str, window, port: int):
    """Create a QSystemTrayIcon (executed on the Qt main loop).

    Uses window.native (BrowserView/QMainWindow) for direct show/hide,
    bypassing the pywebview decorators.
    """

    from PyQt6.QtCore import QCoreApplication
    from PyQt6.QtGui import QAction, QIcon
    from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

    app = QCoreApplication.instance()
    if app is None:
        print("⚠ Tray: no QApplication", flush=True)
        return

    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("⚠ Tray: not available on this desktop", flush=True)
        return

    def _show_window():
        w = window.native
        if w is None:
            return
        if w.isMinimized():
            w.showNormal()
        else:
            w.show()
        w.raise_()
        w.activateWindow()

    def _hide_window():
        w = window.native
        if w is None:
            return
        w.hide()

    icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()
    tray = QSystemTrayIcon(icon)
    tray.setToolTip("OpenVidia")

    menu = QMenu()

    show_action = QAction("Show")
    show_action.triggered.connect(_show_window)
    menu.addAction(show_action)

    hide_action = QAction("Hide dashboard")
    hide_action.triggered.connect(_hide_window)
    menu.addAction(hide_action)

    menu.addSeparator()

    quit_action = QAction("Close dashboard")
    quit_action.triggered.connect(app.quit)
    menu.addAction(quit_action)

    tray.setContextMenu(menu)
    tray.show()
    print("● Tray icon active", flush=True)

    def _on_activated(reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            w = window.native
            if w is not None and w.isVisible() and not w.isMinimized():
                _hide_window()
            else:
                _show_window()
        elif reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            _show_window()

    tray.activated.connect(_on_activated)

    # Global reference to prevent garbage collection of tray/menu/actions
    global _tray_ref, _tray_hide
    _tray_ref = (tray, menu, show_action, hide_action, quit_action, _show_window, _on_activated)
    _tray_hide = _hide_window


def open_desk(port: int) -> None:
    """Open the dashboard in a native pywebview window with a system tray."""
    try:
        import webview
    except ImportError:
        print("⚠ pywebview not installed — opening in browser", flush=True)
        from .webui import auto_open

        auto_open(port)
        return

    # The token rides in the URL, the way Jupyter does it. It cannot be
    # embedded in the page instead: / is unauthenticated (it has to be, or the
    # dashboard could never bootstrap), so anything the HTML carries is
    # readable by whoever can reach the port. main.js moves it straight into
    # sessionStorage and strips it from the address bar.
    url = f"http://localhost:{port}/?token={config.control_token()}"
    assets = Path(__file__).resolve().parent / "web" / "assets"
    icon_path = str(assets / "logo.png")
    print(f"● Desktop window → http://localhost:{port}/", flush=True)

    window = webview.create_window(
        "OpenVidia",
        url=url,
        width=310,
        height=570,
        min_size=(260, 300),
        text_select=True,
        easy_drag=True,
    )

    signaller = _make_signaller(icon_path, window, port)
    # The tray is a QSystemTrayIcon and drives window.native as a Qt window,
    # so the window must run on pywebview's Qt backend too. pywebview picks
    # GTK first on any Linux desktop that is not KDE (Hyprland/Omarchy, GNOME,
    # Sway): the Qt application never existed, the tray never appeared, and
    # close-to-tray hid the window with nothing left to bring it back.
    gui = "qt" if sys.platform.startswith("linux") else None

    def on_closing():
        """Close-to-tray: hide the window, do NOT kill the proxy."""
        if _tray_hide is None:
            return True
        try:
            h = _tray_hide
            if h is not None:
                h()
            else:
                window.hide()
        except Exception:
            pass
        return False

    if signaller is not None:
        window.events.closing += on_closing
        webview.start(
            func=_tray_waiter_factory(signaller, window),
            gui=gui,
            debug=False,
            icon=icon_path,
        )
    else:
        # No tray: closing the view exits only the UI; the proxy is independent.
        webview.start(gui=gui, debug=False, icon=icon_path)


USAGE = f"""OpenVidia — multi-key proxy for NVIDIA NIM

  openvidia                    start the proxy and open the dashboard
  openvidia foreground         start the proxy in this terminal (logs to stdout)
  openvidia update <action>    check, install or roll back a verified update
  openvidia setup              configure opencode + Jcode to use the proxy (backs up first)
  openvidia run <cli> [args]   run another CLI against the proxy, changing no files
  openvidia --help             this message

Dashboard  {ROOT_URL}
API base   {BASE_URL}
Keys       {{keys_path}}

`run` sets the CLI's base-URL variables for that one process only. Override the
variables per CLI in {{targets_path}}."""


def _print_usage() -> None:
    print(
        USAGE.format(
            keys_path=config.config_path(),
            targets_path=config.config_dir() / "cli_targets.json",
        )
    )


def main():
    """CLI entrypoint: dispatch on argv[1]."""
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd in ("-h", "--help", "help"):
            _print_usage()
            return
        if cmd == "setup":
            _setup_cmd()
            return
        if cmd == "run":
            sys.exit(_run_cmd(sys.argv[2:]) or 0)
        if cmd == "update":
            from .updater import main as update_main

            sys.exit(update_main(sys.argv[2:]) or 0)
        if cmd == "foreground":
            asyncio.run(main_async())
            return
        # Unknown arguments used to be ignored silently, which made a typo
        # look like a successful launch.
        print(f"✗ unknown command: {cmd}\n")
        _print_usage()
        sys.exit(2)

    import subprocess as _sp

    if not _proxy_is_serving(PORT):
        if _port_is_open(PORT):
            print(f"Port {PORT} belongs to another or unhealthy service; it was left untouched")
            sys.exit(1)
        options = (
            {"start_new_session": True}
            if sys.platform != "win32"
            else {"creationflags": _sp.CREATE_NEW_PROCESS_GROUP | _sp.DETACHED_PROCESS}
        )
        child = _sp.Popen(
            [sys.executable, "-m", "openvidia", "foreground"],
            stdout=_sp.DEVNULL,
            stderr=_sp.DEVNULL,
            stdin=_sp.DEVNULL,
            **options,
        )
        if not _wait_until_serving(PORT, child, timeout=20.0):
            sys.exit(1)

    open_desk(PORT)


if __name__ == "__main__":
    main()
