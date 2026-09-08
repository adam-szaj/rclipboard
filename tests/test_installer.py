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
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent

# Scripts that must end up in BIN_DIR after installation.
EXPECTED_SCRIPTS = [
    "rclipctl",
    "rclip-smoke.sh",
    "rcliptunel",
    "install-systemd-user.sh",
    "rclipboard-launcher",
    "rclipboard-setup",
]

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
        "    bootstrap) exit \"${LAUNCHCTL_BOOTSTRAP_STATUS:-0}\" ;;\n"
        "esac\n"
    )
    stub.chmod(0o755)
    return log


def _run_systemd_adapter(
    home: Path,
    fake_bin: Path,
    *,
    action: str = "install",
    start_service: bool = True,
    xsel: bool = False,
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
        env={**env, "HOME": str(home)},
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
            "LAUNCHCTL_BOOTSTRAP_STATUS": str(bootstrap_status),
        },
        capture_output=True,
        text=True,
    )


def _run_installer(
    home: Path,
    fake_bin: Path,
    *,
    skip_pip: bool = True,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop("XDG_CONFIG_HOME", None)
    env["HOME"] = str(home)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    if skip_pip:
        env["RCLIPBOARD_INSTALL_SKIP_PIP"] = "1"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(ROOT_DIR / "scripts" / "install-systemd-user.sh"), str(ROOT_DIR)],
        check=True,
        cwd=ROOT_DIR,
        env=env,
        capture_output=True,
        text=True,
    )


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
        _run_installer(cls.home, cls.fake_bin)
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
        _run_installer(home, fake_bin)
        config = app_dir / "config.toml"
        config.write_text("# sentinel\n")
        _run_installer(home, fake_bin)
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
