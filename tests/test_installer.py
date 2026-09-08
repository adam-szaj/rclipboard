"""Installer and systemd/launchd adapter integration tests.

Covers:
  - directory layout and installed scripts
  - config.toml generation from example
  - rendered systemd units using the shared runner and private umask
  - env file output from `rclipboard config env` (via Python interpreter)
  - env file is valid shell syntax
  - env file KEY="value" format
  - idempotency (second install does not overwrite config.toml or env)
"""
from __future__ import annotations

import os
import plistlib
import pty
import re
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent

# Scripts that must end up in BIN_DIR after installation.
EXPECTED_PUBLIC_SCRIPTS = {
    "rclipctl",
    "rcliptunel",
    "rclipboard-launcher",
    "rclipboard-service-run",
    "rclipboard-setup",
    "rclipboard-update",
    "rclipboard-uninstall",
}
EXPECTED_SCRIPTS = sorted(EXPECTED_PUBLIC_SCRIPTS)

EXPECTED_INSTALLER_FILES = {
    "install.sh",
    "install/common.sh",
    "install/systemd.sh",
    "install/launchd.sh",
    "systemd/user/rclipboard.service",
    "systemd/user/rclipboard-display.service",
    "launchd/com.rclipboard.service.plist.in",
    "config/rclipboard.conf.example",
}

# Systemd service units that must be installed.
EXPECTED_UNITS = [
    "rclipboard.service",
    "rclipboard-display.service",
]

def _make_fake_systemctl(fake_bin: Path) -> Path:
    log = fake_bin.parent / "systemctl.log"
    stub = fake_bin / "systemctl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"printf '%s\\n' \"$*\" >> {log}\n"
        "case \"$*\" in\n"
        "    '--user daemon-reload') "
        "exit \"${SYSTEMCTL_DAEMON_STATUS:-${SYSTEMCTL_STATUS:-0}}\" ;;\n"
        "    '--user enable --now rclipboard.service') "
        "exit \"${SYSTEMCTL_ENABLE_MAIN_STATUS:-${SYSTEMCTL_STATUS:-0}}\" ;;\n"
        "    '--user enable --now rclipboard-display.service') "
        "exit \"${SYSTEMCTL_ENABLE_DISPLAY_STATUS:-${SYSTEMCTL_STATUS:-0}}\" ;;\n"
        "esac\n"
        "exit \"${SYSTEMCTL_STATUS:-0}\"\n"
    )
    stub.chmod(0o755)
    return log


def _make_fake_launchctl(fake_bin: Path) -> Path:
    log = fake_bin.parent / "launchctl.log"
    stub = fake_bin / "launchctl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$LAUNCHCTL_LOG\"\n"
        "case \"${1:-}\" in\n"
        "    print) exit \"${LAUNCHCTL_PRINT_STATUS:-0}\" ;;\n"
        "    bootout) exit \"${LAUNCHCTL_BOOTOUT_STATUS:-3}\" ;;\n"
        "    bootstrap) exit \"${LAUNCHCTL_BOOTSTRAP_STATUS:-0}\" ;;\n"
        "esac\n"
    )
    stub.chmod(0o755)
    return log


def _make_fake_python(fake_bin: Path) -> Path:
    stub = fake_bin / "python3"
    stub.write_text(
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = -m ] && [ \"${2:-}\" = venv ]; then\n"
        "    mkdir -p \"$3/bin\"\n"
        "    printf '#!/bin/sh\\nexit 0\\n' > \"$3/bin/python\"\n"
        "    printf '#!/bin/sh\\n[ -z \"${PIP_LOG:-}\" ] || "
        "printf \"%%s\\\\n\" \"$*\" >> \"$PIP_LOG\"\\nexit 0\\n' "
        "> \"$3/bin/pip\"\n"
        "    printf '#!/bin/sh\\nprintf '\"'\"'RCLIPBOARD_XSEL=\\\"0\\\"\\n'\"'\"'\\n' "
        "> \"$3/bin/rclipboard\"\n"
        "    chmod 0755 \"$3/bin/python\" \"$3/bin/pip\" "
        "\"$3/bin/rclipboard\"\n"
        "    exit 0\n"
        "fi\n"
        f'exec "{sys.executable}" "$@"\n'
    )
    stub.chmod(0o755)
    return stub


def _run_systemd_adapter(
    home: Path,
    fake_bin: Path,
    *,
    action: str = "install",
    start_service: bool = True,
    xsel: bool = False,
    enable_display_status: int = 0,
) -> subprocess.CompletedProcess[str]:
    app_dir = home / ".config" / "rclipboard"
    config_dir = app_dir
    fake_rclipboard = app_dir / "venv" / "bin" / "rclipboard"
    fake_rclipboard.parent.mkdir(parents=True, exist_ok=True)
    fake_rclipboard.write_text(
        "#!/bin/sh\n"
        f"printf 'RCLIPBOARD_XSEL=\"{int(xsel)}\"\\n'\n"
    )
    fake_rclipboard.chmod(0o755)
    (config_dir / "config.toml").touch()

    driver = """
set -euo pipefail
APP_DIR=$1
CONFIG_DIR=$2
START_SERVICE=$3
SYSTEMCTL_BIN=$4
PYTHON_BIN=$5
. "$6"
. "$7"
case "$8" in
    install)
        service_preflight
        service_install
        if [ "$START_SERVICE" = 1 ]; then
            service_start
        fi
        ;;
    start) service_start ;;
    stop) service_stop ;;
    restart) service_restart ;;
    status-hint) service_print_status_hint ;;
    uninstall) service_uninstall ;;
    install-conditional)
        if service_install; then
            exit 0
        else
            exit $?
        fi
        ;;
esac
"""
    env = os.environ.copy()
    env.pop("XDG_CONFIG_HOME", None)
    return subprocess.run(
        [
            "bash",
            "-c",
            driver,
            "systemd-adapter-test",
            str(app_dir),
            str(config_dir),
            str(int(start_service)),
            str(fake_bin / "systemctl"),
            sys.executable,
            str(ROOT_DIR / "scripts/install/common.sh"),
            str(ROOT_DIR / "scripts/install/systemd.sh"),
            action,
        ],
        env={
            **env,
            "HOME": str(home),
            "SYSTEMCTL_ENABLE_DISPLAY_STATUS": str(enable_display_status),
        },
        capture_output=True,
        text=True,
    )


def _run_launchd_adapter(
    home: Path,
    fake_bin: Path,
    *,
    action: str = "install",
    start_service: bool = True,
    print_status: int = 0,
    bootout_status: int = 3,
    bootstrap_status: int = 0,
) -> subprocess.CompletedProcess[str]:
    app_dir = home / ".config" / "rclipboard"
    driver = """
set -euo pipefail
APP_DIR=$1
START_SERVICE=$2
LAUNCHCTL_BIN=$3
USER_ID=$4
warn() { printf 'warning: %s\\n' "$*" >&2; }
. "$5"
. "$6"
case "$7" in
    install)
        service_preflight
        service_install
        if [ "$START_SERVICE" = 1 ]; then
            service_start
        fi
        ;;
    start) service_start ;;
    stop) service_stop ;;
    restart) service_restart ;;
    status-hint) service_print_status_hint ;;
    uninstall) service_uninstall ;;
    uninstall-conditional)
        if service_uninstall; then
            exit 0
        else
            exit $?
        fi
        ;;
    install-conditional)
        if service_install; then
            exit 0
        else
            exit $?
        fi
        ;;
esac
"""
    return subprocess.run(
        [
            "bash",
            "-c",
            driver,
            "launchd-adapter-test",
            str(app_dir),
            str(int(start_service)),
            str(fake_bin / "launchctl"),
            str(os.getuid()),
            str(ROOT_DIR / "scripts/install/common.sh"),
            str(ROOT_DIR / "scripts/install/launchd.sh"),
            action,
        ],
        env={
            **os.environ,
            "HOME": str(home),
            "LAUNCHCTL_LOG": str(fake_bin.parent / "launchctl.log"),
            "LAUNCHCTL_PRINT_STATUS": str(print_status),
            "LAUNCHCTL_BOOTOUT_STATUS": str(bootout_status),
            "LAUNCHCTL_BOOTSTRAP_STATUS": str(bootstrap_status),
        },
        capture_output=True,
        text=True,
    )


def _run_installer(
    home: Path,
    fake_bin: Path,
    *,
    platform: str = "Linux",
    args: list[str] | None = None,
    input_text: str | None = None,
    skip_pip: bool = True,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("XDG_CONFIG_HOME", None)
    env["HOME"] = str(home)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["RCLIPBOARD_INSTALL_UNAME"] = platform
    env["SYSTEMCTL_BIN"] = str(fake_bin / "systemctl")
    env["LAUNCHCTL_BIN"] = str(fake_bin / "launchctl")
    env["LAUNCHCTL_LOG"] = str(fake_bin.parent / "launchctl.log")
    if not (fake_bin / "python3").exists():
        _make_fake_python(fake_bin)
    env["PYTHON_BIN"] = str(fake_bin / "python3")
    if skip_pip:
        env["RCLIPBOARD_INSTALL_SKIP_PIP"] = "1"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(ROOT_DIR / "scripts/install.sh"), *(args or [])],
        cwd=ROOT_DIR,
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
    )


def _installer_env(
    home: Path,
    fake_bin: Path,
    *,
    platform: str = "Linux",
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("XDG_CONFIG_HOME", None)
    env["HOME"] = str(home)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["RCLIPBOARD_INSTALL_UNAME"] = platform
    env["SYSTEMCTL_BIN"] = str(fake_bin / "systemctl")
    env["LAUNCHCTL_BIN"] = str(fake_bin / "launchctl")
    env["LAUNCHCTL_LOG"] = str(fake_bin.parent / "launchctl.log")
    env["PYTHON_BIN"] = str(fake_bin / "python3")
    env["RCLIPBOARD_INSTALL_SKIP_PIP"] = "1"
    if extra_env:
        env.update(extra_env)
    return env


def _run_installer_with_tty(
    command: list[str],
    env: dict[str, str],
    typed: bytes,
    *,
    cwd: Path = ROOT_DIR,
) -> subprocess.CompletedProcess[bytes]:
    """Run command with a real controlling terminal and capture combined output."""
    pid, master = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        os.execvpe(command[0], command, env)

    output = bytearray()
    deadline = time.monotonic() + 20
    try:
        if typed:
            os.write(master, typed)
        status: int | None = None
        while status is None:
            if time.monotonic() >= deadline:
                os.kill(pid, 9)
                os.waitpid(pid, 0)
                raise subprocess.TimeoutExpired(command, 20, bytes(output))
            readable, _, _ = select.select([master], [], [], 0.05)
            if readable:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    output.extend(chunk)
            waited, child_status = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                status = child_status
        while True:
            readable, _, _ = select.select([master], [], [], 0)
            if not readable:
                break
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
        return subprocess.CompletedProcess(
            command,
            os.waitstatus_to_exitcode(status),
            bytes(output),
            b"",
        )
    finally:
        os.close(master)


def _read_metadata(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
    )


def _current_branch() -> str:
    return subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=ROOT_DIR,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _run_config_env(config: Path) -> str:
    """Run `rclipboard config env --config FILE` via the current Python interpreter.

    This avoids any dependency on a pip-installed `rclipboard` binary.
    """
    result = subprocess.run(
        [
            sys.executable, "-c",
            (
                "import sys; "
                f"sys.argv = ['rclipboard', 'config', 'env', '--config', {str(config)!r}]; "
                "import rclipboard; rclipboard.main()"
            ),
        ],
        env={**os.environ, "PYTHONPATH": str(ROOT_DIR / "src")},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


# ── layout + units ───────────────────────────────────────────────────────────


class InstallerConfigTests(unittest.TestCase):
    def test_config_examples_are_identical(self) -> None:
        canonical = ROOT_DIR / "scripts/config/rclipboard.conf.example"
        legacy = ROOT_DIR / "scripts/systemd/user/rclipboard.conf.example"
        self.assertTrue(legacy.is_symlink())
        self.assertEqual(
            os.readlink(legacy),
            "../../config/rclipboard.conf.example",
        )
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


class ServiceRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

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


class SystemdAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmp.name)
        self.home = tmp_path / "home"
        self.fake_bin = tmp_path / "fake-bin"
        self.home.mkdir()
        self.fake_bin.mkdir()
        self.systemctl_log = _make_fake_systemctl(self.fake_bin)
        self.app_dir = self.home / ".config" / "rclipboard"
        self.unit_dir = self.home / ".config" / "systemd" / "user"
        result = _run_systemd_adapter(self.home, self.fake_bin)
        self.assertEqual(result.returncode, 0, result.stderr)

    def tearDown(self) -> None:
        self._tmp.cleanup()

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

    def test_linux_install_does_not_enable_display_without_xsel(self) -> None:
        self.assertNotIn(
            "--user enable --now rclipboard-display.service",
            self.systemctl_log.read_text(),
        )

    def test_linux_install_enables_display_when_xsel_is_resolved(self) -> None:
        self.systemctl_log.unlink()
        result = _run_systemd_adapter(
            self.home, self.fake_bin, action="start", xsel=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "--user enable --now rclipboard-display.service",
            self.systemctl_log.read_text(),
        )

    def test_linux_display_enable_failure_is_propagated(self) -> None:
        result = _run_systemd_adapter(
            self.home,
            self.fake_bin,
            action="start",
            xsel=True,
            enable_display_status=8,
        )
        self.assertNotEqual(result.returncode, 0)

    def test_linux_no_start_installs_without_enabling_services(self) -> None:
        self.systemctl_log.unlink()
        result = _run_systemd_adapter(
            self.home, self.fake_bin, start_service=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.systemctl_log.read_text()
        self.assertIn("--user daemon-reload", log)
        self.assertNotIn("enable --now", log)

    def test_linux_unmanaged_collision_is_not_overwritten(self) -> None:
        unit = self.unit_dir / "rclipboard.service"
        original = b"[Unit]\nDescription=user-owned\n"
        unit.write_bytes(original)
        result = _run_systemd_adapter(self.home, self.fake_bin)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(unit.read_bytes(), original)

    def test_linux_conditional_install_does_not_overwrite_collision(self) -> None:
        unit = self.unit_dir / "rclipboard.service"
        original = b"[Unit]\nDescription=user-owned\n"
        unit.write_bytes(original)
        result = _run_systemd_adapter(
            self.home, self.fake_bin, action="install-conditional"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(unit.read_bytes(), original)

    def test_linux_xml_header_does_not_make_second_line_marker_managed(
        self,
    ) -> None:
        unit = self.unit_dir / "rclipboard.service"
        original = (
            b'<?xml version="1.0" encoding="UTF-8"?>\n'
            b"# Generated by rclipboard installer\n"
            b"[Unit]\nDescription=user-owned\n"
        )
        unit.write_bytes(original)
        result = _run_systemd_adapter(
            self.home,
            self.fake_bin,
            action="install-conditional",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(unit.read_bytes(), original)

    def test_linux_uninstall_preserves_second_line_systemd_marker(self) -> None:
        unit = self.unit_dir / "rclipboard.service"
        original = (
            b'<?xml version="1.0" encoding="UTF-8"?>\n'
            b"# Generated by rclipboard installer\n"
            b"[Unit]\nDescription=user-owned\n"
        )
        unit.write_bytes(original)
        result = _run_systemd_adapter(self.home, self.fake_bin, action="uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(unit.read_bytes(), original)

    def test_linux_stop_disables_both_owned_services(self) -> None:
        self.systemctl_log.unlink()
        result = _run_systemd_adapter(self.home, self.fake_bin, action="stop")
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.systemctl_log.read_text()
        self.assertIn(
            "--user disable --now rclipboard-display.service", log
        )
        self.assertIn("--user disable --now rclipboard.service", log)

    def test_linux_restart_restarts_main_service(self) -> None:
        self.systemctl_log.unlink()
        result = _run_systemd_adapter(self.home, self.fake_bin, action="restart")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.systemctl_log.read_text(),
            "--user restart rclipboard.service\n",
        )

    def test_linux_adapter_prints_status_hint(self) -> None:
        result = _run_systemd_adapter(
            self.home,
            self.fake_bin,
            action="status-hint",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "  Status:   systemctl --user status rclipboard.service\n",
        )

    def test_linux_uninstall_removes_marked_units_and_legacy_dropin(self) -> None:
        dropin = self.unit_dir / "rclipboard.service.d"
        dropin.mkdir()
        (dropin / "override.conf").write_text(
            f"[Service]\nWorkingDirectory={self.app_dir}\n"
        )
        self.systemctl_log.unlink()
        result = _run_systemd_adapter(self.home, self.fake_bin, action="uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.unit_dir / "rclipboard.service").exists())
        self.assertFalse(
            (self.unit_dir / "rclipboard-display.service").exists()
        )
        self.assertFalse(dropin.exists())
        log = self.systemctl_log.read_text()
        self.assertIn("--user disable --now rclipboard.service", log)
        self.assertTrue(log.endswith("--user daemon-reload\n"), log)

    def test_linux_uninstall_preserves_unmarked_unit(self) -> None:
        unit = self.unit_dir / "rclipboard.service"
        original = b"[Unit]\nDescription=user-owned\n"
        unit.write_bytes(original)
        result = _run_systemd_adapter(self.home, self.fake_bin, action="uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(unit.read_bytes(), original)


class LaunchdAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmp.name)
        self.home = tmp_path / "home & user"
        self.fake_bin = tmp_path / "fake-bin"
        self.home.mkdir()
        self.fake_bin.mkdir()
        self.launchctl_log = _make_fake_launchctl(self.fake_bin)
        self.app_dir = self.home / ".config" / "rclipboard"
        self.plist_path = (
            self.home
            / "Library/LaunchAgents/com.rclipboard.service.plist"
        )
        result = _run_launchd_adapter(self.home, self.fake_bin)
        self.assertEqual(result.returncode, 0, result.stderr)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_macos_plist_is_valid_and_uses_shared_runner(self) -> None:
        with self.plist_path.open("rb") as f:
            data = plistlib.load(f)
        self.assertEqual(data["Label"], "com.rclipboard.service")
        self.assertEqual(
            data["ProgramArguments"],
            [str(self.app_dir / "bin/rclipboard-service-run")],
        )
        self.assertTrue(data["RunAtLoad"])
        self.assertTrue(data["KeepAlive"])
        self.assertEqual(data["WorkingDirectory"], str(self.app_dir))

    def test_macos_plist_has_marker_before_root(self) -> None:
        text = self.plist_path.read_text()
        marker = "<!-- Generated by rclipboard installer -->"
        self.assertLess(text.index(marker), text.index("<plist"))

    def test_macos_plist_uses_user_log_paths(self) -> None:
        with self.plist_path.open("rb") as f:
            data = plistlib.load(f)
        log_dir = self.home / "Library/Logs/rclipboard"
        self.assertEqual(data["StandardOutPath"], str(log_dir / "stdout.log"))
        self.assertEqual(data["StandardErrorPath"], str(log_dir / "stderr.log"))
        self.assertTrue(log_dir.is_dir())

    def test_macos_install_bootstraps_gui_agent(self) -> None:
        log = self.launchctl_log.read_text()
        domain = f"gui/{os.getuid()}"
        self.assertIn(f"print {domain}", log)
        self.assertIn(f"bootstrap {domain} {self.plist_path}", log)

    def test_macos_unmanaged_collision_is_not_overwritten(self) -> None:
        original = b"user-owned plist\n"
        self.plist_path.write_bytes(original)
        result = _run_launchd_adapter(
            self.home,
            self.fake_bin,
            action="install-conditional",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.plist_path.read_bytes(), original)

    def test_macos_missing_gui_domain_warns_and_succeeds(self) -> None:
        self.launchctl_log.unlink()
        result = _run_launchd_adapter(
            self.home,
            self.fake_bin,
            print_status=1,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "LaunchAgent installed; it will load at the next graphical login",
            result.stderr,
        )
        self.assertNotIn("bootstrap", self.launchctl_log.read_text())

    def test_macos_bootstrap_failure_in_gui_domain_is_an_error(self) -> None:
        result = _run_launchd_adapter(
            self.home,
            self.fake_bin,
            bootstrap_status=23,
        )
        self.assertNotEqual(result.returncode, 0, result.stderr)

    def test_macos_start_propagates_real_bootout_error(self) -> None:
        self.launchctl_log.unlink()
        result = _run_launchd_adapter(
            self.home,
            self.fake_bin,
            action="start",
            bootout_status=5,
        )
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("bootstrap", self.launchctl_log.read_text())

    def test_macos_restart_kickstarts_label_in_gui_domain(self) -> None:
        self.launchctl_log.unlink()
        result = _run_launchd_adapter(
            self.home,
            self.fake_bin,
            action="restart",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        domain = f"gui/{os.getuid()}"
        self.assertEqual(
            self.launchctl_log.read_text(),
            f"print {domain}\nkickstart -k {domain}/com.rclipboard.service\n",
        )

    def test_macos_stop_boots_out_exact_plist(self) -> None:
        self.launchctl_log.unlink()
        result = _run_launchd_adapter(
            self.home,
            self.fake_bin,
            action="stop",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        domain = f"gui/{os.getuid()}"
        self.assertEqual(
            self.launchctl_log.read_text(),
            f"bootout {domain} {self.plist_path}\n",
        )

    def test_macos_adapter_prints_status_hint(self) -> None:
        result = _run_launchd_adapter(
            self.home,
            self.fake_bin,
            action="status-hint",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            f"  Status:   launchctl print gui/{os.getuid()}/"
            "com.rclipboard.service\n",
        )

    def test_macos_stop_propagates_real_bootout_error(self) -> None:
        result = _run_launchd_adapter(
            self.home,
            self.fake_bin,
            action="stop",
            bootout_status=5,
        )
        self.assertNotEqual(result.returncode, 0, result.stderr)

    def test_macos_uninstall_keeps_plist_when_bootout_fails(self) -> None:
        result = _run_launchd_adapter(
            self.home,
            self.fake_bin,
            action="uninstall-conditional",
            bootout_status=5,
        )
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.plist_path.exists())

    def test_macos_uninstall_removes_only_marked_plist(self) -> None:
        result = _run_launchd_adapter(
            self.home,
            self.fake_bin,
            action="uninstall",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.plist_path.exists())

        original = b"user-owned plist\n"
        self.plist_path.write_bytes(original)
        result = _run_launchd_adapter(
            self.home,
            self.fake_bin,
            action="uninstall",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.plist_path.read_bytes(), original)


class UnifiedInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.home = self.tmp_path / "home"
        self.fake_bin = self.tmp_path / "fake-bin"
        self.home.mkdir()
        self.fake_bin.mkdir()
        self.systemctl_log = _make_fake_systemctl(self.fake_bin)
        self.launchctl_log = _make_fake_launchctl(self.fake_bin)
        self.result = _run_installer(
            self.home,
            self.fake_bin,
            args=["--no-start"],
        )
        self.assertEqual(self.result.returncode, 0, self.result.stderr)
        self.app_dir = self.home / ".config/rclipboard"
        self.bin_dir = self.app_dir / "bin"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_only_public_scripts_are_installed(self) -> None:
        installed = {p.name for p in self.bin_dir.iterdir() if p.is_file()}
        self.assertEqual(installed, EXPECTED_PUBLIC_SCRIPTS)

    def test_installer_payload_is_exact_and_has_safe_modes(self) -> None:
        installer_dir = self.app_dir / "installer"
        installed = {
            str(path.relative_to(installer_dir))
            for path in installer_dir.rglob("*")
            if path.is_file()
        }
        self.assertEqual(installed, EXPECTED_INSTALLER_FILES)
        for relative in EXPECTED_INSTALLER_FILES:
            path = installer_dir / relative
            expected_mode = 0o755 if path.suffix == ".sh" else 0o644
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected_mode)
        for name in EXPECTED_PUBLIC_SCRIPTS:
            self.assertEqual(
                stat.S_IMODE((self.bin_dir / name).stat().st_mode),
                0o755,
            )

    def test_metadata_records_repository_remote_and_branch(self) -> None:
        metadata = _read_metadata(self.app_dir / "install.conf")
        self.assertEqual(metadata["repo_dir"], str(ROOT_DIR))
        self.assertEqual(metadata["remote"], "origin")
        self.assertEqual(metadata["branch"], _current_branch())
        self.assertEqual(
            stat.S_IMODE((self.app_dir / "install.conf").stat().st_mode),
            0o600,
        )

    def test_explicit_branch_is_recorded(self) -> None:
        home = self.tmp_path / "branch-home"
        home.mkdir()
        result = _run_installer(
            home,
            self.fake_bin,
            args=["--remote", "origin", "--branch", "main", "--no-start"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        metadata = _read_metadata(home / ".config/rclipboard/install.conf")
        self.assertEqual(metadata["remote"], "origin")
        self.assertEqual(metadata["branch"], "main")

    def test_linux_dispatch_installs_only_systemd_service(self) -> None:
        self.assertTrue(
            (self.home / ".config/systemd/user/rclipboard.service").is_file()
        )
        self.assertFalse(
            (self.home / "Library/LaunchAgents/com.rclipboard.service.plist").exists()
        )
        self.assertIn("--user daemon-reload", self.systemctl_log.read_text())

    def test_no_start_does_not_enable_linux_service(self) -> None:
        self.assertNotIn("enable --now", self.systemctl_log.read_text())

    def test_default_install_starts_linux_service(self) -> None:
        home = self.tmp_path / "started-home"
        home.mkdir()
        self.systemctl_log.unlink()
        result = _run_installer(home, self.fake_bin)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "--user enable --now rclipboard.service",
            self.systemctl_log.read_text(),
        )

    def test_main_service_enable_failure_is_not_reported_as_success(self) -> None:
        home = self.tmp_path / "start-failure-home"
        home.mkdir()
        result = _run_installer(
            home,
            self.fake_bin,
            extra_env={"SYSTEMCTL_ENABLE_MAIN_STATUS": "9"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Installed rclipboard user service.", result.stdout)
        self.assertIn("failed to start systemd service", result.stderr)

    def test_package_is_installed_from_repository(self) -> None:
        home = self.tmp_path / "package-home"
        pip_log = self.tmp_path / "pip.log"
        home.mkdir()
        result = _run_installer(
            home,
            self.fake_bin,
            args=["--no-start"],
            skip_pip=False,
            extra_env={"PIP_LOG": str(pip_log)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(pip_log.read_text(), f"install {ROOT_DIR} --quiet\n")

    def test_metadata_is_not_written_when_service_install_fails(self) -> None:
        home = self.tmp_path / "service-failure-home"
        home.mkdir()
        result = _run_installer(
            home,
            self.fake_bin,
            args=["--no-start"],
            extra_env={"SYSTEMCTL_STATUS": "7"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((home / ".config/rclipboard/install.conf").exists())

    def test_darwin_dispatch_installs_only_launchd_service(self) -> None:
        home = self.tmp_path / "darwin-home"
        home.mkdir()
        result = _run_installer(
            home,
            self.fake_bin,
            platform="Darwin",
            args=["--no-start"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            (home / "Library/LaunchAgents/com.rclipboard.service.plist").is_file()
        )
        self.assertFalse((home / ".config/systemd/user").exists())

    def test_custom_xdg_config_home_is_used(self) -> None:
        home = self.tmp_path / "xdg-home"
        xdg_config = self.tmp_path / "xdg-config"
        home.mkdir()
        result = _run_installer(
            home,
            self.fake_bin,
            args=["--no-start"],
            extra_env={"XDG_CONFIG_HOME": str(xdg_config)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        app_dir = xdg_config / "rclipboard"
        self.assertTrue((app_dir / "install.conf").is_file())
        unit = xdg_config / "systemd/user/rclipboard.service"
        self.assertIn(f"WorkingDirectory={app_dir}", unit.read_text())

    def test_new_config_defaults_to_private_uds(self) -> None:
        with (self.app_dir / "config.toml").open("rb") as file:
            config = tomllib.load(file)
        self.assertEqual(
            config["server"]["endpoint"],
            "uds://${XDG_RUNTIME_DIR}/rclipboard/uds.sock",
        )
        self.assertEqual(config["client"]["transport"], "uds")
        self.assertEqual(
            stat.S_IMODE((self.app_dir / "config.toml").stat().st_mode),
            0o600,
        )

    def test_tcp_is_loopback_only_for_new_config(self) -> None:
        home = self.tmp_path / "tcp-home"
        home.mkdir()
        result = _run_installer(
            home,
            self.fake_bin,
            args=["--transport", "tcp", "--no-start"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        config_path = home / ".config/rclipboard/config.toml"
        with config_path.open("rb") as file:
            config = tomllib.load(file)
        self.assertEqual(config["server"]["endpoint"], "127.0.0.1:8989")
        self.assertEqual(config["client"]["transport"], "tcp")
        self.assertEqual(config["client"]["endpoint"], "127.0.0.1:8989")
        self.assertNotIn("0.0.0.0", config_path.read_text())

    def test_reinstall_preserves_config_and_all_user_keys(self) -> None:
        sentinels = {
            "config.toml": "# custom config\n",
            "age_key.txt": "private\n",
            "age_key.pub": "public\n",
            "known_keys": "known\n",
        }
        for name, content in sentinels.items():
            (self.app_dir / name).write_text(content)
        result = _run_installer(
            self.home,
            self.fake_bin,
            args=["--transport", "tcp", "--no-start"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for name, content in sentinels.items():
            with self.subTest(name=name):
                self.assertEqual((self.app_dir / name).read_text(), content)

    def test_installed_lifecycle_wrappers_delegate_arguments(self) -> None:
        log = self.tmp_path / "delegation.log"
        installer = self.app_dir / "installer/install.sh"
        installer.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$DELEGATION_LOG\"\n"
        )
        installer.chmod(0o755)
        env = {
            **os.environ,
            "HOME": str(self.home),
            "DELEGATION_LOG": str(log),
        }
        update = subprocess.run(
            [str(self.bin_dir / "rclipboard-update"), "--branch", "topic"],
            env=env,
            capture_output=True,
            text=True,
        )
        uninstall = subprocess.run(
            [str(self.bin_dir / "rclipboard-uninstall"), "--purge-user-data"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(update.returncode, 0, update.stderr)
        self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
        self.assertEqual(
            log.read_text(),
            "--update --branch topic\n--uninstall --purge-user-data\n",
        )

    def test_metadata_parser_does_not_evaluate_values(self) -> None:
        touched = self.tmp_path / "evaluated"
        metadata = self.tmp_path / "install.conf"
        metadata.write_text(
            f"repo_dir=$(touch {touched})\nremote=origin\nbranch=main\n"
        )
        result = subprocess.run(
            [
                "bash",
                "-c",
                '. "$1"; read_install_metadata "$2"; printf "%s\\n" "$REPO_DIR"',
                "metadata-test",
                str(ROOT_DIR / "scripts/install/common.sh"),
                str(metadata),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"$(touch {touched})\n")
        self.assertFalse(touched.exists())

    def test_metadata_parser_rejects_invalid_shape(self) -> None:
        cases = {
            "duplicate": "repo_dir=/repo\nremote=origin\nremote=again\nbranch=main\n",
            "unknown": "repo_dir=/repo\nremote=origin\nbranch=main\nextra=value\n",
            "missing": "repo_dir=/repo\nremote=origin\n",
        }
        for name, content in cases.items():
            with self.subTest(name=name):
                metadata = self.tmp_path / f"{name}.conf"
                metadata.write_text(content)
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        '. "$1"; read_install_metadata "$2"',
                        "metadata-test",
                        str(ROOT_DIR / "scripts/install/common.sh"),
                        str(metadata),
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0)

    def test_unsupported_platform_fails_before_app_mutation(self) -> None:
        home = self.tmp_path / "unsupported-home"
        home.mkdir()
        result = _run_installer(
            home,
            self.fake_bin,
            platform="Plan9",
            args=["--no-start"],
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((home / ".config/rclipboard").exists())

    def test_old_python_fails_before_app_mutation(self) -> None:
        home = self.tmp_path / "old-python-home"
        home.mkdir()
        old_python = self.fake_bin / "old-python"
        old_python.write_text("#!/bin/sh\nexit 1\n")
        old_python.chmod(0o755)
        result = _run_installer(
            home,
            self.fake_bin,
            args=["--no-start"],
            extra_env={"PYTHON_BIN": str(old_python)},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((home / ".config/rclipboard").exists())

    def test_missing_remote_fails_before_app_mutation(self) -> None:
        home = self.tmp_path / "missing-remote-home"
        home.mkdir()
        result = _run_installer(
            home,
            self.fake_bin,
            args=["--remote", "does-not-exist", "--no-start"],
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((home / ".config/rclipboard").exists())

    def test_missing_branch_fails_before_app_mutation(self) -> None:
        home = self.tmp_path / "missing-branch-home"
        home.mkdir()
        result = _run_installer(
            home,
            self.fake_bin,
            args=["--branch", "branch-that-does-not-exist", "--no-start"],
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((home / ".config/rclipboard").exists())

    def test_invalid_transport_fails_before_app_mutation(self) -> None:
        home = self.tmp_path / "invalid-transport-home"
        home.mkdir()
        result = _run_installer(
            home,
            self.fake_bin,
            args=["--transport", "auto", "--no-start"],
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((home / ".config/rclipboard").exists())

    def test_detached_head_fails_before_app_mutation(self) -> None:
        home = self.tmp_path / "detached-home"
        home.mkdir()
        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        git_stub = self.fake_bin / "git"
        git_stub.write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  *'symbolic-ref --quiet --short HEAD'*) exit 1 ;;\n"
            "esac\n"
            f'exec "{real_git}" "$@"\n'
        )
        git_stub.chmod(0o755)
        result = _run_installer(
            home,
            self.fake_bin,
            args=["--no-start"],
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((home / ".config/rclipboard").exists())

    def test_explicit_branch_does_not_allow_detached_head(self) -> None:
        home = self.tmp_path / "explicit-detached-home"
        home.mkdir()
        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        git_stub = self.fake_bin / "git"
        git_stub.write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  *'symbolic-ref --quiet --short HEAD'*) exit 1 ;;\n"
            "esac\n"
            f'exec "{real_git}" "$@"\n'
        )
        git_stub.chmod(0o755)
        result = _run_installer(
            home,
            self.fake_bin,
            args=["--branch", "main", "--no-start"],
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((home / ".config/rclipboard").exists())

    def test_bare_repository_fails_before_app_mutation(self) -> None:
        home = self.tmp_path / "bare-home"
        home.mkdir()
        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        git_stub = self.fake_bin / "git"
        git_stub.write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  *'rev-parse --is-bare-repository'*) printf 'true\\n'; exit 0 ;;\n"
            "esac\n"
            f'exec "{real_git}" "$@"\n'
        )
        git_stub.chmod(0o755)
        result = _run_installer(
            home,
            self.fake_bin,
            args=["--no-start"],
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((home / ".config/rclipboard").exists())

    def test_non_checkout_fails_before_app_mutation(self) -> None:
        home = self.tmp_path / "non-checkout-home"
        home.mkdir()
        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        git_stub = self.fake_bin / "git"
        git_stub.write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  *'rev-parse --is-inside-work-tree'*) exit 1 ;;\n"
            "esac\n"
            f'exec "{real_git}" "$@"\n'
        )
        git_stub.chmod(0o755)
        result = _run_installer(
            home,
            self.fake_bin,
            args=["--no-start"],
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((home / ".config/rclipboard").exists())

    def test_unmanaged_service_collision_fails_before_app_mutation(self) -> None:
        home = self.tmp_path / "collision-home"
        unit = home / ".config/systemd/user/rclipboard.service"
        unit.parent.mkdir(parents=True)
        original = b"[Unit]\nDescription=user-owned\n"
        unit.write_bytes(original)
        result = _run_installer(
            home,
            self.fake_bin,
            args=["--no-start"],
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(unit.read_bytes(), original)
        self.assertFalse((home / ".config/rclipboard").exists())

    def test_unmanaged_display_collision_fails_before_app_mutation(self) -> None:
        home = self.tmp_path / "display-collision-home"
        unit = home / ".config/systemd/user/rclipboard-display.service"
        unit.parent.mkdir(parents=True)
        original = b"[Unit]\nDescription=user-owned display\n"
        unit.write_bytes(original)
        result = _run_installer(
            home,
            self.fake_bin,
            args=["--no-start"],
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(unit.read_bytes(), original)
        self.assertFalse((home / ".config/rclipboard").exists())


class _LifecycleTestCase(unittest.TestCase):
    platform = "Linux"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.home = self.tmp_path / "home"
        self.fake_bin = self.tmp_path / "fake-bin"
        self.home.mkdir()
        self.fake_bin.mkdir()
        self.systemctl_log = _make_fake_systemctl(self.fake_bin)
        self.launchctl_log = _make_fake_launchctl(self.fake_bin)
        result = _run_installer(
            self.home,
            self.fake_bin,
            platform=self.platform,
            args=["--no-start"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.app_dir = self.home / ".config/rclipboard"
        self.user_data = {
            "config.toml": b"# custom config\n",
            "age_key.txt": b"private-key\n",
            "age_key.pub": b"public-key\n",
            "known_keys": b"known-key\n",
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_user_data_sentinels(self) -> dict[Path, bytes]:
        sentinels = {}
        for name, content in self.user_data.items():
            path = self.app_dir / name
            path.write_bytes(content)
            sentinels[path] = content
        return sentinels

    def _assert_sentinels_unchanged(self, sentinels: dict[Path, bytes]) -> None:
        for path, content in sentinels.items():
            with self.subTest(path=path):
                self.assertEqual(path.read_bytes(), content)

    def _env(self, extra_env: dict[str, str] | None = None) -> dict[str, str]:
        return _installer_env(
            self.home,
            self.fake_bin,
            platform=self.platform,
            extra_env=extra_env,
        )

    def run_reset(
        self,
        extra: list[str] | None = None,
        *,
        input_text: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return _run_installer(
            self.home,
            self.fake_bin,
            platform=self.platform,
            args=["--reset", "--no-start", *(extra or [])],
            input_text=input_text,
            extra_env=extra_env,
        )

    def run_installed(
        self,
        extra: list[str] | None = None,
        *,
        input_text: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            str(self.app_dir / "bin/rclipboard-uninstall"),
            *(extra or []),
        ]
        return subprocess.run(
            command,
            cwd=self.tmp_path,
            env=self._env(extra_env),
            input=input_text,
            capture_output=True,
            text=True,
        )

    def run_with_tty(
        self,
        command: list[str],
        typed: bytes,
        *,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        return _run_installer_with_tty(
            command,
            self._env(extra_env),
            typed,
        )


class ResetTests(_LifecycleTestCase):
    def test_reset_preserves_user_data_unknown_files_and_recreates_runtime(self) -> None:
        sentinels = self._write_user_data_sentinels()
        unknown = self.app_dir / "notes.txt"
        unknown.write_bytes(b"keep me\n")
        old_python = self.app_dir / "venv/bin/python"
        old_python.write_bytes(b"old runtime\n")

        result = self.run_reset()

        self.assertEqual(result.returncode, 0, result.stderr)
        self._assert_sentinels_unchanged(sentinels)
        self.assertEqual(unknown.read_bytes(), b"keep me\n")
        self.assertTrue(self.app_dir.is_dir())
        self.assertNotEqual(old_python.read_bytes(), b"old runtime\n")

    def test_purge_requires_exact_yes_and_cancels_before_any_mutation(self) -> None:
        sentinels = self._write_user_data_sentinels()
        runtime = self.app_dir / "venv/runtime-sentinel"
        runtime.write_bytes(b"runtime\n")
        service = self.home / ".config/systemd/user/rclipboard.service"
        service_before = service.read_bytes()
        log_before = self.systemctl_log.read_bytes()
        command = [
            "bash",
            str(ROOT_DIR / "scripts/install.sh"),
            "--reset",
            "--purge-user-data",
            "--no-start",
        ]

        for label, typed in {
            "capitalized": b"Yes\n",
            "trailing-space": b"yes \n",
            "blank": b"\n",
            "eof": b"\x04",
        }.items():
            with self.subTest(answer=label):
                result = self.run_with_tty(command, typed)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(b"user data purge cancelled", result.stdout)
                self._assert_sentinels_unchanged(sentinels)
                self.assertEqual(runtime.read_bytes(), b"runtime\n")
                self.assertEqual(service.read_bytes(), service_before)
                self.assertEqual(self.systemctl_log.read_bytes(), log_before)

    def test_piped_yes_cannot_authorize_purge(self) -> None:
        sentinels = self._write_user_data_sentinels()
        runtime = self.app_dir / "venv/runtime-sentinel"
        runtime.write_bytes(b"runtime\n")
        log_before = self.systemctl_log.read_bytes()

        result = self.run_reset(["--purge-user-data"], input_text="yes\n")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires an interactive terminal", result.stderr)
        self._assert_sentinels_unchanged(sentinels)
        self.assertEqual(runtime.read_bytes(), b"runtime\n")
        self.assertEqual(self.systemctl_log.read_bytes(), log_before)

    def test_confirm_lists_every_existing_target_before_exact_yes(self) -> None:
        sentinels = self._write_user_data_sentinels()
        command = [
            "bash",
            str(ROOT_DIR / "scripts/install.sh"),
            "--reset",
            "--purge-user-data",
            "--no-start",
        ]

        result = self.run_with_tty(command, b"no\n")

        self.assertNotEqual(result.returncode, 0)
        output = result.stdout.decode(errors="replace")
        lines = output.splitlines()
        prompt_offset = output.index("Type yes to continue:")
        for path in sentinels:
            self.assertIn(str(path), lines)
            self.assertLess(output.index(str(path)), prompt_offset)

    def test_exact_yes_purges_then_creates_only_fresh_config(self) -> None:
        self._write_user_data_sentinels()
        unknown = self.app_dir / "keep.txt"
        unknown.write_bytes(b"unknown\n")
        command = [
            "bash",
            str(ROOT_DIR / "scripts/install.sh"),
            "--reset",
            "--purge-user-data",
            "--no-start",
        ]

        result = self.run_with_tty(command, b"yes\n")

        self.assertEqual(result.returncode, 0, result.stdout)
        with (self.app_dir / "config.toml").open("rb") as file:
            config = tomllib.load(file)
        self.assertEqual(config["client"]["transport"], "uds")
        for name in ("age_key.txt", "age_key.pub", "known_keys"):
            self.assertFalse((self.app_dir / name).exists())
        self.assertEqual(unknown.read_bytes(), b"unknown\n")

    def test_reset_rejects_unmanaged_service_before_runtime_mutation(self) -> None:
        service = self.home / ".config/systemd/user/rclipboard.service"
        service.write_bytes(b"user-owned\n")
        runtime = self.app_dir / "venv/runtime-sentinel"
        runtime.write_bytes(b"runtime\n")
        log_before = self.systemctl_log.read_bytes()

        result = self.run_reset()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to overwrite unmanaged file", result.stderr)
        self.assertEqual(service.read_bytes(), b"user-owned\n")
        self.assertEqual(runtime.read_bytes(), b"runtime\n")
        self.assertEqual(self.systemctl_log.read_bytes(), log_before)

    def test_service_cleanup_failure_prevents_runtime_removal(self) -> None:
        runtime = self.app_dir / "venv/runtime-sentinel"
        runtime.write_bytes(b"runtime\n")

        result = self.run_reset(extra_env={"SYSTEMCTL_STATUS": "7"})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed to uninstall systemd service", result.stderr)
        self.assertEqual(runtime.read_bytes(), b"runtime\n")


class UninstallTests(_LifecycleTestCase):
    def test_uninstall_preserves_user_data_unknown_files_and_removes_runtime(self) -> None:
        sentinels = self._write_user_data_sentinels()
        unknown_app = self.app_dir / "notes.txt"
        unknown_bin = self.app_dir / "bin/user-command"
        unknown_installer = self.app_dir / "installer/user-note"
        unknown_app.write_bytes(b"app\n")
        unknown_bin.write_bytes(b"bin\n")
        unknown_installer.write_bytes(b"installer\n")
        service = self.home / ".config/systemd/user/rclipboard.service"

        result = self.run_installed()

        self.assertEqual(result.returncode, 0, result.stderr)
        self._assert_sentinels_unchanged(sentinels)
        self.assertFalse((self.app_dir / "venv").exists())
        self.assertFalse((self.app_dir / "install.conf").exists())
        self.assertFalse(service.exists())
        self.assertFalse((self.app_dir / "bin/rclipboard-uninstall").exists())
        self.assertEqual(unknown_app.read_bytes(), b"app\n")
        self.assertEqual(unknown_bin.read_bytes(), b"bin\n")
        self.assertEqual(unknown_installer.read_bytes(), b"installer\n")
        self.assertTrue(self.app_dir.is_dir())

    def test_exact_yes_purges_user_data_and_self_removes_installed_payload(self) -> None:
        self._write_user_data_sentinels()
        wrapper = self.app_dir / "bin/rclipboard-uninstall"
        installed_script = self.app_dir / "installer/install.sh"
        command = [str(wrapper), "--purge-user-data"]

        result = self.run_with_tty(command, b"yes\n")

        self.assertEqual(result.returncode, 0, result.stdout)
        for name in self.user_data:
            self.assertFalse((self.app_dir / name).exists())
        self.assertFalse(wrapper.exists())
        self.assertFalse(installed_script.exists())
        self.assertFalse(self.app_dir.exists())

    def test_uninstall_does_not_require_recorded_checkout_to_exist(self) -> None:
        metadata = self.app_dir / "install.conf"
        metadata.write_text(
            "repo_dir=/checkout/that/no-longer-exists\n"
            "remote=origin\n"
            "branch=main\n"
        )

        result = self.run_installed()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.app_dir / "venv").exists())

    def test_invalid_metadata_cancels_before_service_or_runtime_mutation(self) -> None:
        (self.app_dir / "install.conf").write_text("remote=origin\n")
        runtime = self.app_dir / "venv/runtime-sentinel"
        runtime.write_bytes(b"runtime\n")
        log_before = self.systemctl_log.read_bytes()

        result = self.run_installed()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing metadata key", result.stderr)
        self.assertEqual(runtime.read_bytes(), b"runtime\n")
        self.assertEqual(self.systemctl_log.read_bytes(), log_before)

    def test_uninstall_keeps_unmarked_service_definition(self) -> None:
        service = self.home / ".config/systemd/user/rclipboard.service"
        service.write_bytes(b"user-owned\n")

        result = self.run_installed()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(service.read_bytes(), b"user-owned\n")

    def test_linux_service_cleanup_failure_prevents_runtime_removal(self) -> None:
        runtime = self.app_dir / "venv/runtime-sentinel"
        runtime.write_bytes(b"runtime\n")

        result = self.run_installed(extra_env={"SYSTEMCTL_STATUS": "7"})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed to uninstall systemd service", result.stderr)
        self.assertEqual(runtime.read_bytes(), b"runtime\n")

    def test_cleanup_rejects_unsafe_application_paths(self) -> None:
        for app_dir in ("", "/", str(self.home), "/tmp/not-rclipboard"):
            with self.subTest(app_dir=app_dir):
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        '. "$1"; APP_DIR="$2"; '
                        'VENV_DIR="$APP_DIR/venv"; '
                        'INSTALLER_DIR="$APP_DIR/installer"; '
                        "validate_app_dir_for_cleanup",
                        "cleanup-test",
                        str(ROOT_DIR / "scripts/install/common.sh"),
                        app_dir,
                    ],
                    env={**os.environ, "HOME": str(self.home)},
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unsafe application directory", result.stderr)

    def test_cleanup_rejects_mismatched_managed_paths(self) -> None:
        cases = {
            "bin": ("BIN_DIR", "/tmp/not-the-app-bin"),
            "venv": ("VENV_DIR", "/tmp/not-the-app-venv"),
            "installer": ("INSTALLER_DIR", "/tmp/not-the-app-installer"),
            "metadata": ("METADATA_FILE", "/tmp/not-the-app-metadata"),
        }
        for name, (variable, value) in cases.items():
            with self.subTest(path=name):
                assignments = (
                    f'APP_DIR="{self.app_dir}"; '
                    'BIN_DIR="$APP_DIR/bin"; '
                    'VENV_DIR="$APP_DIR/venv"; '
                    'INSTALLER_DIR="$APP_DIR/installer"; '
                    'METADATA_FILE="$APP_DIR/install.conf"; '
                    f'{variable}="$2"; '
                )
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        f'. "$1"; {assignments} validate_app_dir_for_cleanup',
                        "cleanup-test",
                        str(ROOT_DIR / "scripts/install/common.sh"),
                        value,
                    ],
                    env={**os.environ, "HOME": str(self.home)},
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unsafe", result.stderr)


class DarwinUninstallTests(_LifecycleTestCase):
    platform = "Darwin"

    def test_launchd_cleanup_failure_prevents_runtime_removal(self) -> None:
        runtime = self.app_dir / "venv/runtime-sentinel"
        runtime.write_bytes(b"runtime\n")

        result = self.run_installed(extra_env={"LAUNCHCTL_BOOTOUT_STATUS": "5"})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed to uninstall launchd service", result.stderr)
        self.assertEqual(runtime.read_bytes(), b"runtime\n")

    def test_uninstall_keeps_unmarked_launch_agent(self) -> None:
        plist = self.home / "Library/LaunchAgents/com.rclipboard.service.plist"
        plist.write_bytes(b"user-owned plist\n")

        result = self.run_installed()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(plist.read_bytes(), b"user-owned plist\n")


class InstallerLayoutTests(unittest.TestCase):
    """Directory structure, scripts, and unit files after a fresh install."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(cls._tmp.name)
        cls.home = tmp_path / "home"
        cls.fake_bin = tmp_path / "fake-bin"
        cls.home.mkdir()
        cls.fake_bin.mkdir()
        cls.systemctl_log = _make_fake_systemctl(cls.fake_bin)
        result = _run_installer(cls.home, cls.fake_bin, args=["--no-start"])
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        cls.app_dir = cls.home / ".config" / "rclipboard"
        cls.bin_dir = cls.app_dir / "bin"
        cls.venv_dir = cls.app_dir / "venv"
        cls.unit_dir = cls.home / ".config" / "systemd" / "user"

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    # ── directories ──────────────────────────────────────────────────────────

    def test_bin_dir_exists(self) -> None:
        self.assertTrue(self.bin_dir.is_dir())

    def test_venv_dir_exists(self) -> None:
        self.assertTrue(self.venv_dir.is_dir())

    def test_venv_python_exists(self) -> None:
        self.assertTrue((self.venv_dir / "bin" / "python").exists())

    # ── scripts ──────────────────────────────────────────────────────────────

    def test_expected_scripts_installed_and_executable(self) -> None:
        for name in EXPECTED_SCRIPTS:
            with self.subTest(script=name):
                path = self.bin_dir / name
                self.assertTrue(path.exists(), f"{name} not in bin_dir")
                self.assertTrue(os.access(path, os.X_OK), f"{name} not executable")

    # ── systemd units ─────────────────────────────────────────────────────────

    def test_unit_files_installed(self) -> None:
        for name in EXPECTED_UNITS:
            with self.subTest(unit=name):
                self.assertTrue((self.unit_dir / name).exists(), name)

    def test_main_unit_uses_shared_runner(self) -> None:
        expected = f'ExecStart="{self.app_dir}/bin/rclipboard-service-run"'
        self.assertIn(expected, (self.unit_dir / "rclipboard.service").read_text())

    def test_main_service_has_private_umask(self) -> None:
        text = (self.unit_dir / "rclipboard.service").read_text()
        self.assertIn("UMask=0077", text)

    def test_main_service_has_bounded_stop_timeout(self) -> None:
        text = (self.unit_dir / "rclipboard.service").read_text()
        self.assertIn("TimeoutStopSec=20s", text)

    def test_working_directory_in_unit(self) -> None:
        text = (self.unit_dir / "rclipboard.service").read_text()
        self.assertIn(f"WorkingDirectory={self.app_dir}", text)

    def test_custom_xdg_config_home_is_used_for_app_and_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            fake_bin = tmp_path / "fake-bin"
            xdg_config = tmp_path / "custom-config"
            home.mkdir()
            fake_bin.mkdir()
            _make_fake_systemctl(fake_bin)
            _run_installer(
                home,
                fake_bin,
                args=["--no-start"],
                extra_env={"XDG_CONFIG_HOME": str(xdg_config)},
            )
            app_dir = xdg_config / "rclipboard"
            unit = (
                xdg_config / "systemd/user/rclipboard.service"
            ).read_text()
            self.assertTrue((app_dir / "venv/bin/python").exists())
            self.assertIn(f"WorkingDirectory={app_dir}", unit)
            self.assertIn(
                f'ExecStart="{app_dir}/bin/rclipboard-service-run"', unit
            )

    # ── override.conf ─────────────────────────────────────────────────────────

    def test_legacy_working_directory_override_is_not_created(self) -> None:
        override = self.unit_dir / "rclipboard.service.d" / "override.conf"
        self.assertFalse(override.exists())

    # ── systemctl ─────────────────────────────────────────────────────────────

    def test_daemon_reload_called(self) -> None:
        self.assertTrue(self.systemctl_log.exists())
        self.assertIn("--user daemon-reload", self.systemctl_log.read_text())

    # ── config.toml ───────────────────────────────────────────────────────────

    def test_config_toml_generated(self) -> None:
        self.assertTrue((self.app_dir / "config.toml").exists())

    def test_config_toml_is_valid_toml(self) -> None:
        with (self.app_dir / "config.toml").open("rb") as f:
            data = tomllib.load(f)
        self.assertIn("server", data)
        self.assertIn("proxy", data)

    def test_config_toml_has_endpoint(self) -> None:
        with (self.app_dir / "config.toml").open("rb") as f:
            data = tomllib.load(f)
        self.assertIn("endpoint", data["server"])


# ── idempotency ──────────────────────────────────────────────────────────────


class InstallerIdempotencyTests(unittest.TestCase):
    """Running the installer twice must not overwrite config.toml or env."""

    def _fresh_env(self) -> tuple[Path, Path, Path]:
        tmp_path = Path(self._tmp.name)
        home = tmp_path / "home"
        fake_bin = tmp_path / "fake-bin"
        home.mkdir(exist_ok=True)
        fake_bin.mkdir(exist_ok=True)
        _make_fake_systemctl(fake_bin)
        return home, fake_bin, home / ".config" / "rclipboard"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_second_install_preserves_config_toml(self) -> None:
        home, fake_bin, app_dir = self._fresh_env()
        result = _run_installer(home, fake_bin, args=["--no-start"])
        self.assertEqual(result.returncode, 0, result.stderr)
        config = app_dir / "config.toml"
        config.write_text("# sentinel\n")
        result = _run_installer(home, fake_bin, args=["--no-start"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(config.read_text(), "# sentinel\n")


# ── env file generation ──────────────────────────────────────────────────────


class ConfigEnvOutputTests(unittest.TestCase):
    """Tests for `rclipboard config env` output format and content.

    Invokes the Python module directly (no pip-installed binary needed).
    """

    @classmethod
    def setUpClass(cls) -> None:
        example = (
            ROOT_DIR / "scripts" / "systemd" / "user" / "rclipboard.conf.example"
        )
        cls._tmp = tempfile.TemporaryDirectory()
        cls.config_toml = Path(cls._tmp.name) / "config.toml"
        shutil.copy(example, cls.config_toml)
        cls.env_output = _run_config_env(cls.config_toml)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_output_contains_endpoint(self) -> None:
        self.assertIn("RCLIPBOARD_ENDPOINT=", self.env_output)

    def test_output_is_valid_shell_syntax(self) -> None:
        result = subprocess.run(
            ["bash", "-n"],
            input=self.env_output,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode, 0,
            f"env output has invalid shell syntax:\n{result.stderr}",
        )

    def test_output_key_value_format(self) -> None:
        """Every non-blank, non-comment line must be KEY="..."."""
        pattern = re.compile(r'^[A-Z_]+=".+"$')
        for line in self.env_output.splitlines():
            if not line or line.startswith("#"):
                continue
            with self.subTest(line=line):
                self.assertRegex(line, pattern)

    def test_dollar_sign_escaped_in_output(self) -> None:
        """Raw $ characters must not appear unescaped (would expand in shell)."""
        for line in self.env_output.splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            value = line.split("=", 1)[1]
            # Remove outer quotes then check for bare $
            inner = value.strip('"')
            unescaped = re.findall(r'(?<!\\)\$', inner)
            self.assertEqual(unescaped, [], f"unescaped $ in: {line!r}")


if __name__ == "__main__":
    unittest.main()
