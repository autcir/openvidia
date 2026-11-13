# Installation and release updates

The base package is a headless proxy. It does not install Qt, WebEngine or
pywebview. The HTTP dashboard bundle is included in both installation modes.

```sh
# Reproducible source install, no desktop dependencies:
uv sync --frozen
uv run openvidia foreground

# Native desktop on Linux, macOS or Windows:
uv sync --frozen --extra desktop
.venv/bin/openvidia

# PyPI installations:
python -m pip install openvidia
python -m pip install 'openvidia[desktop]'
```

On Windows, the source environment executable is `.venv\Scripts\openvidia.exe`.
Desktop prerequisites still depend on the platform (WebView2 on Windows and
system WebKit on macOS). A cross-platform package is not evidence that every
window manager, operating system version or upstream model has been tested.

The source installer defaults to desktop mode. `./install.sh --headless` skips
GUI dependencies and menu registration. Configure your own NVIDIA API keys before starting the proxy; installation does not provision keys. `--no-start` installs without launching.
On Linux with a systemd user session, `--service` explicitly installs and enables
a persistent user service. Existing service files are backed up first.

## Update checks

```sh
openvidia update check
openvidia update auto-check on
openvidia update status
openvidia update auto-check off
```

Automatic checking is opt-in. It checks the stable PyPI release at most daily,
does not block startup, and does not install code or interrupt conversations.
The last result is stored in `update-check.json`; `updates.json` stores the policy.
Catalog refresh and application release checks are separate operations.

## Managed release installation

```sh
openvidia update install --desktop
# Or pin an exact stable release:
openvidia update install --version 2.3.0 --desktop
openvidia update rollback
```

The pinned version above is an example: installation succeeds only if that
version has actually been published to PyPI. The updater does not publish or
fetch code from a mutable Git branch.

The updater reads the stable release metadata from PyPI, rejects yanked or
foreign-host wheels, verifies the wheel SHA-256, installs into an isolated
versioned virtual environment, and checks imports, the installed dashboard,
`/health` and CLI startup. Only after these checks pass does it atomically switch
the managed launcher's manifest. A failed candidate is removed; the previous
release remains selected and on disk. A concurrent update cannot acquire the
same update lock. Rollback smoke-checks the previous environment before switching.

The updater prints the absolute **managed launcher** path. It lives under
`~/.local/share/openvidia-managed/` on Linux (`XDG_DATA_HOME` is respected),
`~/Library/Application Support/openvidia-managed/` on macOS, and
`%LOCALAPPDATA%\openvidia-managed\` on Windows. Configure your desktop shortcut
or service to use that launcher to adopt managed releases. An existing source,
pip or distribution-package launcher is deliberately not overwritten.

Activation affects the **next managed launch**. Existing sessions and running
processes keep using their current environment. Restart a managed service during
a maintenance window after checking ongoing sessions. This is deliberate:
unattended in-place dependency replacement cannot promise uninterrupted streams.
Automatic application installation/restart is not enabled by the check policy.
Distribution-managed installs should continue to update through their package
manager unless explicitly migrated to this managed launcher.

PyPI metadata and wheels are authenticated over HTTPS; the wheel digest detects
corruption or a mismatch against that metadata. This is not a separately signed
release chain. Dependencies are resolved by pip from the package requirements;
source deployments use `uv.lock` for reproducible dependency versions.


A systemd user unit template for the managed launcher is included in
`packaging/systemd/openvidia-managed.service`. Install it as
`~/.config/systemd/user/openvidia.service` after a managed release has been
prepared, adjusting `ExecStart` if `XDG_DATA_HOME` differs. The unit uses
HTTP/1.1 upstream connections, bounded shutdown, automatic restart and private
file permissions. After selecting an update or rollback, restarting this unit
uses the newly selected managed environment. Installing the template does not
by itself enable it or promise operation after logout; that also depends on the
host's user-session/linger policy.
