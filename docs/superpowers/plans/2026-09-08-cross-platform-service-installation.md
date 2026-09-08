# Cross-Platform Service Installation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the duplicated Linux-only installation flow with one user-level installer that supports systemd and launchd, safe Git updates, reset, and uninstall while preserving user configuration and keys by default.

**Architecture:** `scripts/install.sh` orchestrates lifecycle operations through a small common shell module and one selected platform adapter. Both service managers invoke the same installed runtime wrapper, while installed update and uninstall commands delegate back to a complete installer payload stored below the application directory.

**Tech Stack:** Bash 3.2-compatible shell, Python 3.11 virtual environments, Git, systemd user units, macOS launchd property lists, Python `unittest`, `plistlib`, and Docker-based systemd integration tests.

**Spec:** `docs/superpowers/specs/2026-09-08-cross-platform-service-installation-design.md`

## Global Constraints

- Installation is user-local and must never require `sudo`.
- Supported targets are Linux with `systemctl --user` and macOS with `launchctl`.
- Python 3.11 or newer and a non-bare Git checkout are required.
- The canonical application directory is `${XDG_CONFIG_HOME:-$HOME/.config}/rclipboard`.
- New installations use a private UDS endpoint by default; TCP is explicit and binds only `127.0.0.1:8989`.
- Install, update, ordinary reset, and ordinary uninstall preserve `config.toml`, `age_key.txt`, `age_key.pub`, and `known_keys`.
- Purging user data requires an interactive terminal and literal lowercase `yes`; no bypass flag or environment variable is allowed.
- Update accepts only a clean worktree and a fast-forward from a configurable remote and branch, initially `origin` and the current branch.
- Managed service files carry an ownership marker; unmarked collisions are never overwritten or removed.
- Lifecycle code must remain compatible with the Bash 3.2 language supported by stock macOS.
- Do not change application protocols, native clipboard integration, `rclip-systemctl`, or remote deployment behavior.

---

## File Map

### New files

- `scripts/config/rclipboard.conf.example` — canonical cross-platform starter configuration.
- `scripts/install/common.sh` — shared paths, validation, metadata, manifest, confirmation, and cleanup functions.
- `scripts/install/systemd.sh` — implementation of the service adapter contract for Linux.
- `scripts/install/launchd.sh` — implementation of the service adapter contract for macOS.
- `scripts/launchd/com.rclipboard.service.plist.in` — marked LaunchAgent template.
- `scripts/bin/rclipboard-service-run` — common service entry point and runtime environment generator.
- `scripts/bin/rclipboard-update` — installed entry point for the Git-backed update operation.
- `scripts/bin/rclipboard-uninstall` — installed entry point for uninstall.
- `tests/test_installer_update.py` — isolated Git remote/update behavior tests.
- `docs/INSTALL.md` — consolidated Linux/macOS lifecycle and smoke-test guide.

### Modified files

- `scripts/install.sh` — platform-neutral argument parser and operation orchestrator.
- `scripts/install-systemd-user.sh` — deprecated compatibility wrapper only.
- `scripts/systemd/user/rclipboard.service` — marked unit that invokes the shared runner.
- `scripts/systemd/user/rclipboard-display.service` — ownership marker and unchanged X11 purpose.
- `scripts/systemd/user/rclipboard.conf.example` — compatibility copy of the canonical template.
- `scripts/bin/rclipctl` — canonical config directory and runtime fallback.
- `scripts/bin/rcliptunel` — same config/runtime lookup as the service runner.
- `scripts/bin/rclipboard-launcher` — portable delegation to the shared runner.
- `scripts/bin/rclipboard-setup` — Bash 3.2-compatible prompts, no stale FIFO section, portable paths.
- `tests/test_installer.py` — portable install/reset/uninstall and service-adapter coverage.
- `tests/test_integration_deploy.py` — align legacy direct-installer assertions with the canonical bin directory.
- `docker/test-install/Dockerfile` — add Git for installer preconditions.
- `docker/test-install/Dockerfile.systemd` — add Git and retain real-systemd health verification.
- `docker/test-install/systemd-test.sh` — initialize test Git metadata and use the unified installer.
- `Makefile` — platform-neutral lifecycle targets and legacy aliases.
- `README.md` — one installation summary linked to the detailed guide.
- `docs/CONFIGURE.md` — Linux/macOS service instructions and explicit TCP fallback.

---

### Task 1: Establish the shared configuration and runtime contract

**Files:**
- Create: `scripts/config/rclipboard.conf.example`
- Create: `scripts/bin/rclipboard-service-run`
- Modify: `scripts/systemd/user/rclipboard.conf.example`
- Modify: `scripts/bin/rclipctl:250-280`
- Modify: `scripts/bin/rcliptunel:12-20,70-110`
- Test: `tests/test_installer.py`

**Interfaces:**
- Produces: `rclipboard-service-run`, invoked with no arguments and ending in `exec <app-dir>/venv/bin/rclipboard`.
- Produces: environment contract `RCLIPBOARD_CONFIG=<app-dir>/config.toml` and `${XDG_RUNTIME_DIR:-$HOME/.local/run}/rclipboard/env`.
- Produces: canonical starter config with `[server].endpoint`, `[client].transport`, `[proxy].enabled = false`, and `[xsel].enabled = false`.
- Consumes: `XDG_CONFIG_HOME`, `XDG_RUNTIME_DIR`, `HOME`, and the installed venv executable.

- [ ] **Step 1: Add failing tests for the canonical configuration**

Add tests that parse both configuration example paths and require identical,
safe defaults:

```python
def test_config_examples_are_identical(self) -> None:
    canonical = ROOT_DIR / "scripts/config/rclipboard.conf.example"
    legacy = ROOT_DIR / "scripts/systemd/user/rclipboard.conf.example"
    self.assertEqual(canonical.read_bytes(), legacy.read_bytes())

def test_config_defaults_to_uds_without_proxy_or_xsel(self) -> None:
    with (ROOT_DIR / "scripts/config/rclipboard.conf.example").open("rb") as f:
        data = tomllib.load(f)
    self.assertEqual(
        data["server"]["endpoint"],
        "uds://${XDG_RUNTIME_DIR}/rclipboard/uds.sock",
    )
    self.assertEqual(data["client"]["transport"], "uds")
    self.assertFalse(data["proxy"]["enabled"])
    self.assertFalse(data["xsel"]["enabled"])
```

- [ ] **Step 2: Add a failing service-runner test using a fake installed binary**

Build a temporary `<app-dir>/venv/bin/rclipboard` that prints config env when
called with `config env` and records the final environment otherwise:

```python
def test_service_runner_creates_private_runtime_env(self) -> None:
    app_dir = self.home / ".config/rclipboard"
    fake = app_dir / "venv/bin/rclipboard"
    fake.parent.mkdir(parents=True)
    fake.write_text(
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = config ]; then\n"
        "  printf 'RCLIPBOARD_ENDPOINT=\"uds:///tmp/test.sock\"\\n'\n"
        "else\n"
        "  printf '%s\\n' \"$RCLIPBOARD_CONFIG\" > \"$RUN_LOG\"\n"
        "fi\n"
    )
    fake.chmod(0o755)
    runtime_root = self.home / "runtime"
    result = subprocess.run(
        ["bash", str(ROOT_DIR / "scripts/bin/rclipboard-service-run")],
        env={
            **os.environ,
            "HOME": str(self.home),
            "XDG_RUNTIME_DIR": str(runtime_root),
            "RUN_LOG": str(self.home / "runner.log"),
        },
        capture_output=True,
        text=True,
    )
    self.assertEqual(result.returncode, 0, result.stderr)
    env_file = runtime_root / "rclipboard/env"
    self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)
    self.assertEqual(
        (self.home / "runner.log").read_text().strip(),
        str(app_dir / "config.toml"),
    )
```

- [ ] **Step 3: Run the focused tests and confirm they fail**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_installer.InstallerConfigTests \
  tests.test_installer.ServiceRunnerTests -v
```

Expected: failure because the canonical template and service runner do not yet
exist, followed by lookup/default failures in the client scripts.

- [ ] **Step 4: Create the canonical safe configuration**

Move the maintained configuration content to
`scripts/config/rclipboard.conf.example`, remove the obsolete `[fifo]` section,
and set these exact defaults:

```toml
[server]
endpoint = "uds://${XDG_RUNTIME_DIR}/rclipboard/uds.sock"

[xsel]
enabled = false

[proxy]
enabled = false

[client]
transport = "uds"
```

Retain the existing logging, synchronization, retention, SSL, and encryption
fields. Make the legacy systemd example byte-for-byte equal to the canonical
file so compatibility is testable.

- [ ] **Step 5: Implement the shared service runner**

Implement the runner with quoted paths, `umask 077`, a temporary env file, and
atomic rename:

```bash
#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/rclipboard"
RUNTIME_ROOT="${XDG_RUNTIME_DIR:-$HOME/.local/run}"
RUNTIME_DIR="$RUNTIME_ROOT/rclipboard"
ENV_FILE="$RUNTIME_DIR/env"
RCLIPBOARD_BIN="$APP_DIR/venv/bin/rclipboard"

umask 077
mkdir -p "$RUNTIME_DIR"
chmod 0700 "$RUNTIME_DIR"
export XDG_RUNTIME_DIR="$RUNTIME_ROOT"
export RCLIPBOARD_CONFIG="$APP_DIR/config.toml"

"$RCLIPBOARD_BIN" config env --config "$RCLIPBOARD_CONFIG" > "$ENV_FILE.tmp"
chmod 0600 "$ENV_FILE.tmp"
mv -f "$ENV_FILE.tmp" "$ENV_FILE"
exec "$RCLIPBOARD_BIN"
```

Add a trap that removes only `"$ENV_FILE.tmp"` if env generation fails.

- [ ] **Step 6: Align `rclipctl` and `rcliptunel` lookups**

Both scripts must begin configuration resolution with:

```bash
RCLIP_CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/rclipboard"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-$HOME/.local/run}"
_RUNTIME_ENV="$XDG_RUNTIME_DIR/rclipboard/env"
```

Replace hard-coded `~/.config/rclipboard` fallback paths in the touched lookup
blocks with `$RCLIP_CONFIG_DIR`. Do not refactor command parsing or transport
logic.

- [ ] **Step 7: Run focused and adjacent client tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_installer.InstallerConfigTests \
  tests.test_installer.ServiceRunnerTests \
  tests.test_rclipctl_transport -v
```

Expected: all tests pass.

- [ ] **Step 8: Commit the shared runtime contract**

```bash
git add scripts/config/rclipboard.conf.example \
  scripts/systemd/user/rclipboard.conf.example \
  scripts/bin/rclipboard-service-run scripts/bin/rclipctl \
  scripts/bin/rcliptunel tests/test_installer.py
git commit -m "feat(installer): add shared runtime contract"
```

---

### Task 2: Implement the systemd service adapter

**Files:**
- Create: `scripts/install/systemd.sh`
- Modify: `scripts/systemd/user/rclipboard.service`
- Modify: `scripts/systemd/user/rclipboard-display.service`
- Test: `tests/test_installer.py`

**Interfaces:**
- Consumes globals: `APP_DIR`, `CONFIG_DIR`, `START_SERVICE`, `SYSTEMCTL_BIN`, `PYTHON_BIN`.
- Produces functions: `service_preflight`, `service_install`, `service_start`, `service_stop`, `service_restart`, `service_uninstall`.
- Produces constant: `SERVICE_PLATFORM=systemd`.
- Uses marker: `# Generated by rclipboard installer` as the first line of every managed unit.

- [ ] **Step 1: Add failing unit-rendering and command tests**

Extend the fake-systemctl harness and assert the adapter installs only the two
owned units and calls the expected user-manager operations:

```python
def test_linux_unit_uses_shared_runner_and_private_umask(self) -> None:
    unit = (self.unit_dir / "rclipboard.service").read_text()
    self.assertTrue(unit.startswith("# Generated by rclipboard installer\n"))
    self.assertIn(
        f'ExecStart="{self.app_dir}/bin/rclipboard-service-run"', unit
    )
    self.assertIn("UMask=0077", unit)
    self.assertIn("TimeoutStopSec=20s", unit)

def test_linux_install_enables_main_service(self) -> None:
    log = self.systemctl_log.read_text()
    self.assertIn("--user daemon-reload", log)
    self.assertIn("--user enable --now rclipboard.service", log)
```

Add an unmanaged-collision case that pre-creates an unmarked
`rclipboard.service`, runs the adapter, expects a nonzero status, and verifies
the original bytes are unchanged.

- [ ] **Step 2: Run the Linux adapter tests and confirm failure**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_installer.SystemdAdapterTests -v
```

Expected: failure because `scripts/install/systemd.sh` and the new ownership
markers do not exist.

- [ ] **Step 3: Update the systemd units**

Make `rclipboard.service` a rendered template with these essential lines:

```ini
# Generated by rclipboard installer
[Unit]
Description=rclipboard service (user)
After=default.target

[Service]
Type=simple
WorkingDirectory=@APP_DIR@
ExecStart="@APP_DIR@/bin/rclipboard-service-run"
Restart=on-failure
RestartSec=1s
TimeoutStopSec=20s
UMask=0077

[Install]
WantedBy=default.target
```

Add the ownership marker to `rclipboard-display.service` without changing its
X11 session semantics.

- [ ] **Step 4: Implement the adapter functions**

Use the overridable command consistently:

```bash
SERVICE_PLATFORM=systemd
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
SYSTEMD_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SYSTEMD_MARKER="# Generated by rclipboard installer"

service_start() {
    "$SYSTEMCTL_BIN" --user enable --now rclipboard.service
}

service_stop() {
    "$SYSTEMCTL_BIN" --user disable --now rclipboard-display.service \
        >/dev/null 2>&1 || true
    "$SYSTEMCTL_BIN" --user disable --now rclipboard.service \
        >/dev/null 2>&1 || true
}

service_restart() {
    "$SYSTEMCTL_BIN" --user restart rclipboard.service
}
```

`service_preflight` must run the shared collision check without modifying any
file. `service_install` must render `@APP_DIR@` and call `daemon-reload` without
starting the service. The common orchestrator calls `service_start` only when
`START_SERVICE=1`.
`service_uninstall` must stop first, delete only marked unit files, remove the
now-empty drop-in directory left by the old installer if its override contains
only the old generated `WorkingDirectory`, and call `daemon-reload` again.

Within `service_start`, enable `rclipboard-display.service` only when resolved
configuration output contains the exact line `RCLIPBOARD_XSEL="1"`. Otherwise
start only the main service.

- [ ] **Step 5: Run adapter and real unit syntax checks**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_installer.SystemdAdapterTests -v
systemd-analyze --user verify scripts/systemd/user/rclipboard.service \
  scripts/systemd/user/rclipboard-display.service
```

Expected: tests pass. If `systemd-analyze --user` cannot connect to a user
manager, rerun `systemd-analyze verify` without `--user` and record that
environment limitation.

- [ ] **Step 6: Commit the Linux adapter**

```bash
git add scripts/install/systemd.sh scripts/systemd/user/rclipboard.service \
  scripts/systemd/user/rclipboard-display.service tests/test_installer.py
git commit -m "feat(installer): add systemd service adapter"
```

---

### Task 3: Implement the launchd service adapter

**Files:**
- Create: `scripts/install/launchd.sh`
- Create: `scripts/launchd/com.rclipboard.service.plist.in`
- Test: `tests/test_installer.py`

**Interfaces:**
- Consumes globals: `APP_DIR`, `START_SERVICE`, `LAUNCHCTL_BIN`, `USER_ID`.
- Produces functions: `service_preflight`, `service_install`, `service_start`, `service_stop`, `service_restart`, `service_uninstall`.
- Produces constant: `SERVICE_PLATFORM=launchd`.
- Uses label: `com.rclipboard.service`.
- Uses marker: `<!-- Generated by rclipboard installer -->` before the plist root.

- [ ] **Step 1: Add failing launchd tests with a fake `launchctl`**

Create a fake command that logs arguments and returns success for
`print gui/<uid>` by default. Add assertions:

```python
def test_macos_plist_is_valid_and_uses_shared_runner(self) -> None:
    plist_path = self.home / "Library/LaunchAgents/com.rclipboard.service.plist"
    with plist_path.open("rb") as f:
        data = plistlib.load(f)
    self.assertEqual(data["Label"], "com.rclipboard.service")
    self.assertEqual(
        data["ProgramArguments"],
        [str(self.app_dir / "bin/rclipboard-service-run")],
    )
    self.assertTrue(data["RunAtLoad"])
    self.assertTrue(data["KeepAlive"])
    self.assertEqual(data["WorkingDirectory"], str(self.app_dir))

def test_macos_install_bootstraps_gui_agent(self) -> None:
    log = self.launchctl_log.read_text()
    domain = f"gui/{os.getuid()}"
    self.assertIn(f"print {domain}", log)
    self.assertIn(f"bootstrap {domain} ", log)
```

Add tests for log paths, unmarked collision refusal, a missing GUI domain that
returns success with a warning, and a bootstrap error in an existing GUI domain
that returns nonzero.

- [ ] **Step 2: Run launchd tests and confirm failure**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_installer.LaunchdAdapterTests -v
```

Expected: failure because neither launchd file exists.

- [ ] **Step 3: Add the marked LaunchAgent template**

Use an XML plist containing this data model:

```xml
<!-- Generated by rclipboard installer -->
<key>Label</key>
<string>com.rclipboard.service</string>
<key>ProgramArguments</key>
<array><string>@SERVICE_RUNNER_XML@</string></array>
<key>WorkingDirectory</key>
<string>@APP_DIR_XML@</string>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>@STDOUT_LOG_XML@</string>
<key>StandardErrorPath</key><string>@STDERR_LOG_XML@</string>
```

Wrap those keys in the standard plist declaration and top-level dictionary.
Use XML-escaped replacement values when rendering; never interpolate raw paths
into XML.

- [ ] **Step 4: Implement launchd lifecycle functions**

Start with the exact platform constants and domain helper:

```bash
SERVICE_PLATFORM=launchd
LAUNCHCTL_BIN="${LAUNCHCTL_BIN:-launchctl}"
USER_ID="${USER_ID:-$(id -u)}"
LAUNCHD_LABEL="com.rclipboard.service"
LAUNCHD_DOMAIN="gui/$USER_ID"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
LAUNCHD_PLIST="$LAUNCHD_DIR/$LAUNCHD_LABEL.plist"
LAUNCHD_MARKER="<!-- Generated by rclipboard installer -->"

launchd_gui_available() {
    "$LAUNCHCTL_BIN" print "$LAUNCHD_DOMAIN" >/dev/null 2>&1
}
```

`service_preflight` checks the target plist ownership without changing it.
`service_install` creates `~/Library/LaunchAgents` and
`~/Library/Logs/rclipboard`, checks ownership, and renders the plist without
loading it. `service_start` then performs:

```bash
if launchd_gui_available; then
    "$LAUNCHCTL_BIN" bootout "$LAUNCHD_DOMAIN" "$LAUNCHD_PLIST" \
        >/dev/null 2>&1 || true
    "$LAUNCHCTL_BIN" bootstrap "$LAUNCHD_DOMAIN" "$LAUNCHD_PLIST"
else
    warn "LaunchAgent installed; it will load at the next graphical login"
fi
```

`service_restart` must use
`launchctl kickstart -k "$LAUNCHD_DOMAIN/$LAUNCHD_LABEL"` when the GUI domain
exists. `service_stop` and `service_uninstall` boot out the exact plist only;
uninstall removes it only if marked.

- [ ] **Step 5: Run launchd adapter tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_installer.LaunchdAdapterTests -v
```

Expected: all tests pass, including XML paths containing `&` in the temporary
home directory.

- [ ] **Step 6: Commit the macOS adapter**

```bash
git add scripts/install/launchd.sh \
  scripts/launchd/com.rclipboard.service.plist.in tests/test_installer.py
git commit -m "feat(installer): add launchd service adapter"
```

---

### Task 4: Build the unified idempotent installer

**Files:**
- Create: `scripts/install/common.sh`
- Create: `scripts/bin/rclipboard-update`
- Create: `scripts/bin/rclipboard-uninstall`
- Modify: `scripts/install.sh`
- Test: `tests/test_installer.py`

**Interfaces:**
- Produces installer operations: default install, `--reset`, and internal `--uninstall` and `--update` dispatch.
- Produces parsed globals: `REPO_DIR`, `APP_DIR`, `BIN_DIR`, `VENV_DIR`, `REMOTE`, `BRANCH`, `TRANSPORT`, `START_SERVICE`, `PURGE_USER_DATA`.
- Produces metadata API: `read_install_metadata <path>` and `write_install_metadata <path> <repo> <remote> <branch>`.
- Consumes one adapter selected from `uname -s`: `Linux -> systemd.sh`, `Darwin -> launchd.sh`.

- [ ] **Step 1: Replace the old installer test harness with a platform-neutral runner**

The helper must allow platform and service tools to be injected without
modifying production command syntax:

```python
def _run_installer(
    home: Path,
    fake_bin: Path,
    *,
    platform: str = "Linux",
    args: list[str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "RCLIPBOARD_INSTALL_SKIP_PIP": "1",
        "RCLIPBOARD_INSTALL_UNAME": platform,
        "SYSTEMCTL_BIN": str(fake_bin / "systemctl"),
        "LAUNCHCTL_BIN": str(fake_bin / "launchctl"),
    }
    return subprocess.run(
        ["bash", str(ROOT_DIR / "scripts/install.sh"), *(args or [])],
        cwd=ROOT_DIR,
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
    )
```

`RCLIPBOARD_INSTALL_UNAME` is a test-only platform override. It may accept only
the exact values `Linux` and `Darwin`; normal users rely on `uname -s`.

- [ ] **Step 2: Add failing install-layout, metadata, and idempotency tests**

Assert the exact public manifest and absence of accidental scripts:

```python
EXPECTED_PUBLIC_SCRIPTS = {
    "rclipctl",
    "rcliptunel",
    "rclipboard-launcher",
    "rclipboard-service-run",
    "rclipboard-setup",
    "rclipboard-update",
    "rclipboard-uninstall",
}

def test_only_public_scripts_are_installed(self) -> None:
    installed = {p.name for p in self.bin_dir.iterdir() if p.is_file()}
    self.assertEqual(installed, EXPECTED_PUBLIC_SCRIPTS)

def test_metadata_records_repository_remote_and_branch(self) -> None:
    metadata = _read_metadata(self.app_dir / "install.conf")
    self.assertEqual(metadata["repo_dir"], str(ROOT_DIR))
    self.assertEqual(metadata["remote"], "origin")
    self.assertEqual(metadata["branch"], _current_branch())
    self.assertEqual(
        stat.S_IMODE((self.app_dir / "install.conf").stat().st_mode), 0o600
    )
```

Keep sentinel tests for `config.toml`, all three key files, and `known_keys` on
a second install. Add `XDG_CONFIG_HOME` coverage and unsupported-platform,
Python-version, detached-HEAD, missing-remote, and unmarked-unit collision tests.

- [ ] **Step 3: Run unified installer tests and confirm failure**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_installer.UnifiedInstallTests -v
```

Expected: failures from the old positional-only installer, broad script copy,
missing metadata, and absent platform dispatch.

- [ ] **Step 4: Implement common validation and metadata functions**

Use these named functions in `scripts/install/common.sh`:

```bash
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
warn() { printf 'warning: %s\n' "$*" >&2; }

resolve_app_dir() {
    printf '%s/rclipboard\n' "${XDG_CONFIG_HOME:-$HOME/.config}"
}

require_python_311() {
    "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' \
        || die "Python 3.11 or newer is required"
}

require_git_checkout() {
    git -C "$REPO_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
        || die "source directory is not a Git checkout: $REPO_DIR"
}
```

Implement metadata parsing with `while IFS='=' read -r key value`; accept only
`repo_dir`, `remote`, and `branch`, reject duplicate/missing keys and values
containing newline characters, and never `source` or `eval` metadata.

Write metadata to a same-directory temporary file under `umask 077`, then rename
it over `install.conf` only after the installation succeeds.

- [ ] **Step 5: Implement the explicit install manifests**

Use fixed newline-separated manifests rather than globs:

```bash
PUBLIC_SCRIPTS='rclipctl
rcliptunel
rclipboard-launcher
rclipboard-service-run
rclipboard-setup
rclipboard-update
rclipboard-uninstall'

INSTALLER_FILES='install.sh
install/common.sh
install/systemd.sh
install/launchd.sh
systemd/user/rclipboard.service
systemd/user/rclipboard-display.service
launchd/com.rclipboard.service.plist.in
config/rclipboard.conf.example'
```

Copy public scripts to `$APP_DIR/bin` as mode `0755`. Copy a self-contained
installer payload below `$APP_DIR/installer`, preserving relative directories;
make its shell entry points executable and templates mode `0644`.

- [ ] **Step 6: Implement argument parsing and platform dispatch**

The parser accepts only:

```text
--remote NAME
--branch NAME
--transport uds|tcp
--no-start
--reset
--purge-user-data
--uninstall        internal installed-wrapper entry
--update           internal installed-wrapper entry
-h|--help
```

Reject `--purge-user-data` without `--reset` or `--uninstall`. Retain the old
optional repository positional argument only inside
`install-systemd-user.sh`; direct `install.sh` derives the repository as the
parent of its own `scripts` directory.

Select exactly one adapter:

```bash
case "${RCLIPBOARD_INSTALL_UNAME:-$(uname -s)}" in
    Linux)  . "$SCRIPT_DIR/install/systemd.sh" ;;
    Darwin) . "$SCRIPT_DIR/install/launchd.sh" ;;
    *)      die "unsupported operating system" ;;
esac
```

This dispatch must occur only after common validation and before mutations.

- [ ] **Step 7: Implement idempotent installation order**

Use this sequence:

```text
parse arguments
resolve and validate paths/platform/Python/Git/remote/branch
check all service-file collisions
create app directories with restrictive umask
create venv if absent
pip install the repository unless RCLIPBOARD_INSTALL_SKIP_PIP=1
copy the explicit public and installer manifests
create config from canonical template only if absent
render/install the platform service
write metadata atomically
start service unless --no-start
print paths and platform-specific status command
```

For `--transport tcp`, transform only a newly created config to the exact
loopback values from the spec using the validated Python interpreter and
`tomllib`-independent line replacements. Never modify an existing config.

- [ ] **Step 8: Add thin installed lifecycle entry points**

`rclipboard-update` must contain only path resolution and delegation:

```bash
#!/usr/bin/env bash
set -euo pipefail
APP_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/rclipboard"
exec "$APP_DIR/installer/install.sh" --update "$@"
```

`rclipboard-uninstall` is identical except for `--uninstall`. The installed
copy of `install.sh` must resolve its installer-root layout as well as the source
repository layout; update reads the recorded source before fetching.

- [ ] **Step 9: Run install tests on both simulated platforms**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_installer.UnifiedInstallTests \
  tests.test_installer.SystemdAdapterTests \
  tests.test_installer.LaunchdAdapterTests -v
```

Expected: all tests pass.

- [ ] **Step 10: Commit the unified installer**

```bash
git add scripts/install.sh scripts/install/common.sh \
  scripts/bin/rclipboard-update scripts/bin/rclipboard-uninstall \
  tests/test_installer.py
git commit -m "feat(installer): unify user service installation"
```

---

### Task 5: Add protected reset and uninstall operations

**Files:**
- Modify: `scripts/install.sh`
- Modify: `scripts/install/common.sh`
- Modify: `tests/test_installer.py`

**Interfaces:**
- Consumes: adapter `service_uninstall` and the explicit managed manifests.
- Produces: `confirm_user_data_purge`, `remove_managed_runtime`, `purge_user_data`, and reset/uninstall orchestration.
- Deletes user data only from the fixed basename set `config.toml age_key.txt age_key.pub known_keys`.

- [ ] **Step 1: Add a pseudo-terminal helper for destructive confirmation tests**

Use `pty.openpty()` so successful purge coverage exercises a real terminal and
does not add a production bypass:

```python
def _run_installer_with_tty(
    command: list[str], env: dict[str, str], typed: bytes
) -> subprocess.CompletedProcess[bytes]:
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            command,
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        os.write(master, typed)
        stdout, stderr = proc.communicate(timeout=20)
        return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
    finally:
        os.close(master)
        os.close(slave)
```

- [ ] **Step 2: Add failing reset and uninstall preservation tests**

Cover these exact outcomes:

```python
def test_reset_preserves_all_user_data(self) -> None:
    sentinels = self._write_user_data_sentinels()
    result = self.run_installer(["--reset", "--no-start"])
    self.assertEqual(result.returncode, 0, result.stderr)
    self._assert_sentinels_unchanged(sentinels)
    self.assertTrue((self.app_dir / "venv").is_dir())

def test_uninstall_preserves_data_and_removes_runtime(self) -> None:
    sentinels = self._write_user_data_sentinels()
    result = self.run_installed("rclipboard-uninstall")
    self.assertEqual(result.returncode, 0, result.stderr)
    self._assert_sentinels_unchanged(sentinels)
    self.assertFalse((self.app_dir / "venv").exists())
    self.assertFalse(self.service_path.exists())
```

Also assert unknown files remain and the application directory remains when it
contains user data.

- [ ] **Step 3: Add failing purge-guard tests**

Test exact `yes`, `Yes`, `yes `, blank input, EOF, and piped/non-TTY `yes`.
Only exact `yes` through the pseudo-terminal may delete the four named files.
Before confirmation, stderr/stdout must contain every existing target path.

For purging reset, assert a new safe `config.toml` is created after deletion and
the three key artifacts stay absent. For purging uninstall, assert the config
and key artifacts stay absent.

- [ ] **Step 4: Run lifecycle tests and confirm failure**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_installer.ResetTests \
  tests.test_installer.UninstallTests -v
```

Expected: failure because the operation implementations do not exist.

- [ ] **Step 5: Implement the purge confirmation**

Use `/dev/tty` rather than stdin so a pipe cannot satisfy the requirement:

```bash
confirm_user_data_purge() {
    [ -t 0 ] && [ -r /dev/tty ] || die \
        "purging configuration and keys requires an interactive terminal"
    printf '%s\n' "The following user data will be deleted:" >&2
    print_existing_user_data_paths >&2
    printf '%s' 'Type yes to continue: ' >/dev/tty
    IFS= read -r answer </dev/tty || die "user data purge cancelled"
    [ "$answer" = "yes" ] || die "user data purge cancelled"
}
```

Call this before stopping the service or removing any managed file, ensuring a
rejected purge makes no changes at all.

- [ ] **Step 6: Implement exact managed cleanup**

Validate that `APP_DIR` is nonempty, absolute, not `/`, not `$HOME`, and ends in
`/rclipboard` before cleanup. Remove only:

```text
<app-dir>/venv
<app-dir>/bin/<each PUBLIC_SCRIPTS entry>
<app-dir>/installer/<each INSTALLER_FILES entry>
<app-dir>/install.conf
the marked service definitions returned by the active adapter
```

Recursive deletion is allowed only for the validated, exact managed venv and
installer directories. Use `rmdir` for parent directories so unknown files
prevent directory removal.

- [ ] **Step 7: Implement reset and uninstall ordering**

For reset:

```text
validate everything needed for the subsequent install
confirm purge when requested
service_uninstall
remove managed runtime
purge exact user-data files when requested
invoke the normal install path from the current source checkout
```

For uninstall:

```text
load and validate metadata
confirm purge when requested
service_uninstall
remove managed runtime except the executing shell process
purge exact user-data files when requested
remove empty directories
```

- [ ] **Step 8: Run the complete lifecycle tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_installer -v
```

Expected: all installer, service, reset, and uninstall tests pass.

- [ ] **Step 9: Commit protected lifecycle removal**

```bash
git add scripts/install.sh scripts/install/common.sh tests/test_installer.py
git commit -m "feat(installer): add protected reset and uninstall"
```

---

### Task 6: Add safe Git-backed updates

**Files:**
- Modify: `scripts/install.sh`
- Modify: `scripts/install/common.sh`
- Create: `tests/test_installer_update.py`

**Interfaces:**
- Consumes: `install.conf`, `git`, the source checkout, and normal install refresh.
- Produces: `perform_update <remote> <branch>` and installed `rclipboard-update` behavior.
- Guarantees: no stash, checkout, rebase, implicit merge, or hard reset.

- [ ] **Step 1: Create local Git repository fixtures**

Create a bare remote, seed checkout, and install-home fixture entirely below a
temporary directory:

```python
def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()

def make_remote_fixture(root: Path) -> tuple[Path, Path, Path]:
    remote = root / "remote.git"
    seed = root / "seed"
    checkout = root / "checkout"
    _git("init", "--bare", str(remote), cwd=root)
    _git("init", "-b", "main", str(seed), cwd=root)
    _git("config", "user.name", "Installer Test", cwd=seed)
    _git("config", "user.email", "installer@example.invalid", cwd=seed)
    # Copy the repository files needed by the installer into seed here.
    _git("add", ".", cwd=seed)
    _git("commit", "-m", "initial", cwd=seed)
    _git("remote", "add", "origin", str(remote), cwd=seed)
    _git("push", "-u", "origin", "main", cwd=seed)
    _git("clone", str(remote), str(checkout), cwd=root)
    return remote, seed, checkout
```

Use `shutil.copytree` with explicit exclusions for `.git`, `.venv`, and cache
directories; do not call shell `cp` with globs.

- [ ] **Step 2: Add a failing successful-update test**

After initial `--no-start` installation, commit and push a sentinel file from
the seed repository, run the installed update command, and assert:

```python
self.assertEqual(result.returncode, 0, result.stderr)
self.assertTrue((checkout / "update-sentinel").exists())
self.assertEqual(_git("status", "--porcelain", cwd=checkout), "")
self.assertIn("restart rclipboard.service", systemctl_log.read_text())
```

Use `RCLIPBOARD_INSTALL_SKIP_PIP=1` but still assert the refreshed installer
payload contains the newly committed installer version marker.

- [ ] **Step 3: Add failing update rejection tests**

Add one test each for:

- an untracked file;
- a tracked modification;
- detached HEAD;
- checked-out branch different from selected branch;
- missing remote;
- missing remote branch;
- diverged local and remote commits;
- failed package refresh, using a fake venv pip that exits nonzero.

Every rejected pre-fetch case must leave `HEAD`, metadata, and service log
unchanged. A post-fast-forward package failure must leave the checkout at the
new commit, leave recorded remote/branch unchanged, and omit the restart call.

- [ ] **Step 4: Add failing remote/branch persistence tests**

Create a second remote named `mirror` and branch `stable`. Invoke:

```bash
rclipboard-update --remote mirror --branch stable
```

After success, assert `install.conf` records `mirror` and `stable`. Run the next
update without flags and assert its Git trace uses those values. If installation
fails after fetch, assert the previous metadata values remain.

- [ ] **Step 5: Run update tests and confirm failure**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_installer_update -v
```

Expected: failure because `--update` has no implementation.

- [ ] **Step 6: Implement strict update validation**

Use Git's own validators and resolved refs:

```bash
git -C "$REPO_DIR" remote get-url "$REMOTE" >/dev/null 2>&1 \
    || die "Git remote does not exist: $REMOTE"
git check-ref-format --branch "$BRANCH" >/dev/null 2>&1 \
    || die "invalid branch name: $BRANCH"
[ -z "$(git -C "$REPO_DIR" status --porcelain)" ] \
    || die "update requires a clean worktree"
[ "$(git -C "$REPO_DIR" symbolic-ref --short HEAD 2>/dev/null)" = "$BRANCH" ] \
    || die "update requires branch $BRANCH to be checked out"
```

Fetch the explicit target and verify it before mutation:

```bash
git -C "$REPO_DIR" fetch "$REMOTE" "$BRANCH"
REMOTE_REF="refs/remotes/$REMOTE/$BRANCH"
git -C "$REPO_DIR" rev-parse --verify "$REMOTE_REF^{commit}" >/dev/null
git -C "$REPO_DIR" merge-base --is-ancestor HEAD "$REMOTE_REF" \
    || die "local and remote history cannot be fast-forwarded"
git -C "$REPO_DIR" merge --ff-only "$REMOTE_REF"
```

- [ ] **Step 7: Refresh using the newly fetched installer**

After the merge, execute the source checkout's new installer with an internal
refresh flag that skips Git mutation but performs validation, package install,
manifest refresh, service rendering, restart, and finally metadata write:

```bash
exec "$REPO_DIR/scripts/install.sh" \
    --internal-refresh \
    --remote "$REMOTE" \
    --branch "$BRANCH"
```

Keep `--internal-refresh` undocumented and reject it unless metadata identifies
the same absolute repository. For this operation only, defer metadata replacement
until the service restart succeeds. If restart fails, return nonzero and retain
the previous remote and branch values even though the checkout and installed
payload already changed.

- [ ] **Step 8: Run update and full installer tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_installer_update tests.test_installer -v
```

Expected: all tests pass.

- [ ] **Step 9: Commit Git update support**

```bash
git add scripts/install.sh scripts/install/common.sh \
  tests/test_installer_update.py
git commit -m "feat(installer): add fast-forward Git updates"
```

---

### Task 7: Make installed helper scripts macOS-compatible

**Files:**
- Modify: `scripts/bin/rclipboard-launcher`
- Modify: `scripts/bin/rclipboard-setup`
- Modify: `tests/test_installer.py`

**Interfaces:**
- `rclipboard-launcher [daemonize]` delegates to `rclipboard-service-run` without `realpath`.
- `rclipboard-setup` runs under Bash 3.2-compatible syntax and writes the canonical config path.
- The setup wizard retains its current interactive feature set, except obsolete FIFO output is removed.

- [ ] **Step 1: Add failing portability assertions**

Add a source scan limited to installed shell scripts:

```python
def test_installed_scripts_avoid_known_bash4_only_constructs(self) -> None:
    for path in INSTALLED_SOURCE_SCRIPTS:
        text = path.read_text()
        self.assertNotRegex(text, r"\$\{[^}]+,,\}")
        self.assertNotIn("declare -A", text)
        self.assertNotIn("mapfile", text)

def test_launcher_does_not_require_realpath(self) -> None:
    text = (ROOT_DIR / "scripts/bin/rclipboard-launcher").read_text()
    self.assertNotIn("realpath", text)
    self.assertIn("rclipboard-service-run", text)
```

Add a wizard test that supplies `1` plus default answers, then parses the
created TOML and asserts no `[fifo]` section exists.

- [ ] **Step 2: Run portability tests and confirm failure**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_installer.HelperPortabilityTests -v
```

Expected: failure on `realpath`, `${reply,,}`, and obsolete wizard output.

- [ ] **Step 3: Reduce the launcher to portable delegation**

Replace the process-detection and recursive daemonization logic with:

```bash
#!/usr/bin/env bash
set -euo pipefail
APP_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/rclipboard"
RUNNER="$APP_DIR/bin/rclipboard-service-run"

if [ "${1:-}" = "daemonize" ]; then
    nohup "$RUNNER" >/dev/null 2>&1 &
    exit 0
fi
exec "$RUNNER"
```

Service managers remain responsible for avoiding duplicate managed processes.

- [ ] **Step 4: Make wizard prompts Bash 3.2-compatible and path-consistent**

Replace lowercase expansion with a `case`:

```bash
prompt_yn() {
    local q="$1" def="$2" reply hint
    [ "$def" = "y" ] && hint="(Y/n)" || hint="(y/N)"
    read -r -p "$q $hint: " reply || true
    reply="${reply:-$def}"
    case "$reply" in
        y|Y) return 0 ;;
        *)   return 1 ;;
    esac
}
```

Remove the stale `[fifo]` block and update summary paths to `$APP_DIR/bin`.
Keep UDS as option 1 and loopback TCP as option 2. On Darwin, present X11/xsel
as unavailable by default without adding native clipboard behavior.

- [ ] **Step 5: Run helper and installer tests**

Run:

```bash
bash -n scripts/bin/rclipboard-launcher scripts/bin/rclipboard-setup
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_installer.HelperPortabilityTests tests.test_installer -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit helper portability fixes**

```bash
git add scripts/bin/rclipboard-launcher scripts/bin/rclipboard-setup \
  tests/test_installer.py
git commit -m "fix(installer): support stock macOS shell tools"
```

---

### Task 8: Preserve compatibility and update build/test entry points

**Files:**
- Modify: `scripts/install-systemd-user.sh`
- Modify: `Makefile`
- Modify: `tests/test_integration_deploy.py`
- Modify: `docker/test-install/Dockerfile`
- Modify: `docker/test-install/Dockerfile.systemd`
- Modify: `docker/test-install/systemd-test.sh`
- Test: `tests/test_installer.py`

**Interfaces:**
- Legacy `scripts/install-systemd-user.sh [REPO_DIR]` delegates to the unified installer and forces Linux/systemd only for its historical test override.
- Make targets: `service-install`, `service-update`, `service-reset`, `service-uninstall`.
- Existing `systemd-user-install` remains a labeled compatibility alias.

- [ ] **Step 1: Add a failing compatibility-wrapper test**

Invoke the old entry point with its positional repository argument and assert:

```python
self.assertEqual(result.returncode, 0, result.stderr)
self.assertIn("deprecated", result.stderr.lower())
self.assertTrue((app_dir / "install.conf").exists())
self.assertIn("enable --now rclipboard.service", systemctl_log.read_text())
```

Also assert the old script contains no venv, copy, or unit-installation logic of
its own.

- [ ] **Step 2: Run the compatibility test and confirm failure**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_installer.LegacyEntryPointTests -v
```

Expected: failure because the old script still duplicates installation.

- [ ] **Step 3: Replace the legacy installer with a thin wrapper**

Use portable repository resolution and delegation:

```bash
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_DIR="${1:-$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)}"
printf '%s\n' \
  'warning: install-systemd-user.sh is deprecated; use scripts/install.sh' >&2
exec "$REPO_DIR/scripts/install.sh"
```

Do not force a fake platform in production; the legacy name is accepted only on
Linux and naturally fails on macOS through the unified detector if someone uses
it there.

- [ ] **Step 4: Add platform-neutral Make targets**

Add these exact definitions and recipes without changing the development
`install: .venv` target:

```make
USER_CONFIG_HOME := $(if $(XDG_CONFIG_HOME),$(XDG_CONFIG_HOME),$(HOME)/.config)

service-install:
	./scripts/install.sh

service-update:
	$(USER_CONFIG_HOME)/rclipboard/bin/rclipboard-update

service-reset:
	./scripts/install.sh --reset

service-uninstall:
	$(USER_CONFIG_HOME)/rclipboard/bin/rclipboard-uninstall

systemd-user-install: service-install
	@echo "systemd-user-install is a compatibility alias for service-install"
```

Make `install-no-systemd` a compatibility alias for
`./scripts/install.sh --no-start` and describe accurately that a service file is
installed but not activated.

- [ ] **Step 5: Align Docker installer fixtures with Git preconditions**

Install `git` in both installer images. Before running the unified installer in
copied source trees, initialize a local repository and remote name:

```bash
git init -b main
git config user.name 'Installer Test'
git config user.email 'installer@example.invalid'
git add .
git commit -m fixture
git remote add origin .
```

In the no-real-systemd integration test, pass `--no-start` and provide the fake
systemctl command. Update assertions from `/root/bin` to
`/root/.config/rclipboard/bin` and assert the two installed lifecycle commands
exist.

- [ ] **Step 6: Update the real-systemd smoke harness**

Run the unified installer instead of the legacy implementation and remove the
now-redundant explicit `systemctl --user start`. Retain the UDS health loop and
final stop. Add assertions that `rclipboard-update` and
`rclipboard-uninstall` are executable.

- [ ] **Step 7: Run compatibility and non-privileged integration tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_installer tests.test_integration_deploy.InstallScriptTests -v
```

If Docker is unavailable, the Docker-backed class may skip; the portable
installer suite must still pass.

- [ ] **Step 8: Commit build and compatibility changes**

```bash
git add scripts/install-systemd-user.sh Makefile \
  tests/test_integration_deploy.py docker/test-install/Dockerfile \
  docker/test-install/Dockerfile.systemd docker/test-install/systemd-test.sh \
  tests/test_installer.py
git commit -m "chore(installer): align compatibility and test entry points"
```

---

### Task 9: Consolidate installation documentation

**Files:**
- Create: `docs/INSTALL.md`
- Modify: `README.md:37-108,674-704`
- Modify: `docs/CONFIGURE.md:1-65`

**Interfaces:**
- Documents the public commands from the spec verbatim.
- Documents Linux status through `systemctl --user status rclipboard.service`.
- Documents macOS status through `launchctl print gui/$(id -u)/com.rclipboard.service`.
- Documents real-macOS smoke verification without claiming it was run unless it was.

- [ ] **Step 1: Add a failing documentation contract test**

Add a small test in `tests/test_installer.py` that requires the detailed guide
to contain the lifecycle commands and safety language:

```python
def test_install_guide_documents_lifecycle_contract(self) -> None:
    text = (ROOT_DIR / "docs/INSTALL.md").read_text()
    for required in (
        "./scripts/install.sh",
        "rclipboard-update",
        "rclipboard-uninstall",
        "--reset",
        "--purge-user-data",
        "127.0.0.1:8989",
        "com.rclipboard.service",
        "rclipboard.service",
        "Type `yes`",
    ):
        self.assertIn(required, text)
```

- [ ] **Step 2: Run the documentation contract test and confirm failure**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_installer.DocumentationContractTests -v
```

Expected: failure because `docs/INSTALL.md` does not exist.

- [ ] **Step 3: Write the detailed installation guide**

Use these sections in order:

```text
Requirements
Install from a Git checkout
Installed layout and PATH
Linux systemd user service
macOS LaunchAgent
Default UDS transport
Loopback TCP fallback for SSH tunneling
Update remote and branch
Reset installation
Uninstall
Configuration/key deletion safeguard
Troubleshooting
Real macOS smoke procedure
```

Show both status commands, both log locations, `--no-start`, the exact
fast-forward/clean-worktree restriction, and the exact interactive lowercase
`yes` requirement. State explicitly that macOS receives the server service but
not native macOS clipboard synchronization.

- [ ] **Step 4: Consolidate README and configuration references**

Replace duplicate/stale systemd sections in README with a concise install
example and link to `docs/INSTALL.md`. Remove claims about nonexistent
`rclipboard.socket` and `rclipboard-proxy.service` units. Update
`docs/CONFIGURE.md` to show UDS first, explicit loopback TCP fallback, and both
service restart commands.

- [ ] **Step 5: Run documentation and config tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_installer.DocumentationContractTests \
  tests.test_installer.InstallerConfigTests -v
git diff --check
```

Expected: all tests pass and Git reports no whitespace errors.

- [ ] **Step 6: Commit documentation**

```bash
git add docs/INSTALL.md README.md docs/CONFIGURE.md tests/test_installer.py
git commit -m "docs: document Linux and macOS service lifecycle"
```

---

### Task 10: Run full verification and record platform limits

**Files:**
- Modify only if verification exposes a defect in files already touched by this plan.

**Interfaces:**
- Consumes all deliverables from Tasks 1-9.
- Produces an evidence-backed completion report that distinguishes portable plist tests from real macOS execution.

- [ ] **Step 1: Run shell syntax checks**

Run:

```bash
bash -n scripts/install.sh scripts/install-systemd-user.sh \
  scripts/install/common.sh scripts/install/systemd.sh scripts/install/launchd.sh \
  scripts/bin/rclipboard-service-run scripts/bin/rclipboard-update \
  scripts/bin/rclipboard-uninstall scripts/bin/rclipboard-launcher \
  scripts/bin/rclipboard-setup scripts/bin/rclipctl scripts/bin/rcliptunel
```

Expected: exit status 0 and no output.

- [ ] **Step 2: Run all portable installer/update tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_installer tests.test_installer_update -v
```

Expected: all cases pass.

- [ ] **Step 3: Run adjacent client tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_rclipctl_transport tests.test_endpoint_config \
  tests.test_envutil -v
```

Expected: all cases pass.

- [ ] **Step 4: Run Docker layout tests**

Run:

```bash
make test-install-docker
```

Expected: image builds and the portable installer suite passes in a clean Linux
container.

- [ ] **Step 5: Run real-systemd service and health verification**

Run:

```bash
make test-install-docker-systemd
```

Expected: the unit loads, starts automatically from the unified installer,
answers `/v1/health.get` through the default UDS, and stops cleanly.

- [ ] **Step 6: Run repository-wide regression tests**

Run:

```bash
make test
```

Expected: all functional, integration, and SSL tests pass. If an optional
external dependency makes a pre-existing suite unavailable, record the exact
command and output and run every unaffected suite separately.

- [ ] **Step 7: Validate the final diff**

Run:

```bash
git diff --check
git status --short
git diff --stat HEAD~9..HEAD
```

Expected: no whitespace errors; only files named in this plan are changed; any
pre-existing unrelated file such as `claude.resume` remains untouched.

- [ ] **Step 8: Perform or explicitly defer real macOS smoke verification**

On a macOS host, follow `docs/INSTALL.md` to run install, `launchctl print`, UDS
health, restart, update from a temporary remote, ordinary reset, ordinary
uninstall, and one purge-cancellation test. If no macOS host is available, state
exactly: "LaunchAgent plist structure and launchctl commands are covered by
portable tests; execution on a real macOS launchd session was not run."

- [ ] **Step 9: Commit a verification correction only when one exists**

If verification required a correction, rerun the failing command after adding
its regression test, stage exactly the files changed for that correction, and
commit them with message `fix(installer): address verification findings`. If no
correction was needed, do not create an empty commit.
