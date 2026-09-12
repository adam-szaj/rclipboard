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
INSTALLED_SOURCE_SCRIPTS = [
    ROOT_DIR / "scripts" / "bin" / name for name in EXPECTED_SCRIPTS
]

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
        "    '--user disable --now rclipboard.service') "
        "exit \"${SYSTEMCTL_DISABLE_MAIN_STATUS:-${SYSTEMCTL_STATUS:-0}}\" ;;\n"
        "    '--user disable --now rclipboard-display.service') "
        "exit \"${SYSTEMCTL_DISABLE_DISPLAY_STATUS:-${SYSTEMCTL_STATUS:-0}}\" ;;\n"
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


def _make_failing_rm(fake_bin: Path, rejected_path: Path) -> None:
    real_rm = shutil.which("rm")
    if real_rm is None:
        raise AssertionError("rm is required for installer tests")
    stub = fake_bin / "rm"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "for argument in \"$@\"; do\n"
        f"    [ \"$argument\" != {str(rejected_path)!r} ] || exit 23\n"
        "done\n"
        f'exec "{real_rm}" "$@"\n'
    )
    stub.chmod(0o755)


def _run_systemd_adapter(
    home: Path,
    fake_bin: Path,
    *,
    action: str = "install",
    start_service: bool = True,
    xsel: bool = False,
    enable_display_status: int = 0,
    disable_main_status: int = 0,
    disable_display_status: int = 0,
    daemon_status: int = 0,
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
            "PATH": f"{fake_bin}:{env['PATH']}",
            "SYSTEMCTL_ENABLE_DISPLAY_STATUS": str(enable_display_status),
            "SYSTEMCTL_DISABLE_MAIN_STATUS": str(disable_main_status),
            "SYSTEMCTL_DISABLE_DISPLAY_STATUS": str(disable_display_status),
            "SYSTEMCTL_DAEMON_STATUS": str(daemon_status),
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


class DocumentationContractTests(unittest.TestCase):
    def test_config_template_describes_cross_platform_service_reload(self) -> None:
        text = (ROOT_DIR / "scripts/config/rclipboard.conf.example").read_text()
        self.assertIn("shared service runner", text)
        self.assertIn("systemctl --user restart rclipboard.service", text)
        self.assertIn(
            "launchctl kickstart -k gui/$(id -u)/com.rclipboard.service",
            text,
        )
        self.assertNotIn("ExecStartPre", text)

    def test_install_guide_has_required_sections_in_order(self) -> None:
        text = (ROOT_DIR / "docs/INSTALL.md").read_text()
        sections = (
            "Requirements",
            "Install from a Git checkout",
            "Installed layout and PATH",
            "Linux systemd user service",
            "macOS LaunchAgent",
            "Default UDS transport",
            "Loopback TCP fallback for SSH tunneling",
            "Update remote and branch",
            "Reset installation",
            "Uninstall",
            "Configuration/key deletion safeguard",
            "Troubleshooting",
            "Real macOS smoke procedure",
        )
        positions = [text.index(f"## {section}") for section in sections]
        self.assertEqual(positions, sorted(positions))

    def test_install_guide_documents_lifecycle_and_transport_contract(self) -> None:
        text = (ROOT_DIR / "docs/INSTALL.md").read_text()
        normalized = " ".join(text.split())
        for required in (
            "./scripts/install.sh",
            "./scripts/install.sh --no-start",
            "./scripts/install.sh --transport tcp",
            "rclipboard-update [--remote REMOTE] [--branch BRANCH]",
            "./scripts/install.sh --reset [--purge-user-data]",
            "rclipboard-uninstall [--purge-user-data]",
            "127.0.0.1:8989",
            "systemctl --user status rclipboard.service",
            "launchctl print gui/$(id -u)/com.rclipboard.service",
            "~/Library/Logs/rclipboard/stdout.log",
            "~/Library/Logs/rclipboard/stderr.log",
            "config.toml",
            "age_key.txt",
            "age_key.pub",
            "known_keys",
            "literal lowercase `yes`",
        ):
            self.assertIn(required, normalized)
        self.assertIn("no automatic UDS-to-TCP fallback", normalized)
        self.assertIn("does not rewrite an existing `config.toml`", normalized)

    def test_install_guide_documents_update_safety_and_platform_limits(self) -> None:
        text = (ROOT_DIR / "docs/INSTALL.md").read_text()
        normalized = " ".join(text.split())
        for required in (
            "attached selected branch",
            "completely clean worktree",
            "fast-forward only",
            "does not stash, rebase, reset, or switch branches",
            "`origin`",
            "checkout was updated but installation refresh failed",
            "server service only",
            "no native macOS clipboard synchronization",
            "xsel publisher remains Linux/X11-only",
            "required before claiming real launchd validation",
            "has not been run as part of the Linux automated test suite",
        ):
            self.assertIn(required, normalized)

    def test_primary_docs_do_not_advertise_stale_service_layout(self) -> None:
        readme = (ROOT_DIR / "README.md").read_text()
        configure = (ROOT_DIR / "docs/CONFIGURE.md").read_text()
        install = (ROOT_DIR / "docs/INSTALL.md").read_text()
        combined = readme + configure + install
        for stale in (
            "rclipboard.socket",
            "rclipboard-proxy.service",
            "~/.local/bin/rclipctl",
            "~/.config/rclipboard/env",
            "./scripts/rclipctl",
            "./scripts/install-systemd-user.sh",
        ):
            self.assertNotIn(stale, combined)
        self.assertIn("docs/INSTALL.md", readme)
        self.assertIn("INSTALL.md", configure)


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
        self.assertFalse(data["pasteboard"]["enabled"])
        self.assertEqual(data["pasteboard"]["pbcopy_path"], "/usr/bin/pbcopy")
        self.assertEqual(data["pasteboard"]["pbpaste_path"], "/usr/bin/pbpaste")
        self.assertEqual(data["pasteboard"]["interval_ms"], 250)


class HelperPortabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmp.name)
        self.home = tmp_path / "home"
        self.xdg_config = tmp_path / "config root"
        self.fake_bin = tmp_path / "fake-bin"
        self.home.mkdir()
        self.fake_bin.mkdir()
        self.app_dir = self.xdg_config / "rclipboard"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_wizard(
        self,
        input_text: str,
        *,
        platform: str | None = None,
        xsel_available: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        if platform is not None:
            uname = self.fake_bin / "uname"
            uname.write_text(f"#!/bin/sh\nprintf '%s\\n' {platform!r}\n")
            uname.chmod(0o755)
        if xsel_available:
            xsel = self.fake_bin / "xsel"
            xsel.write_text("#!/bin/sh\nexit 0\n")
            xsel.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.home),
                "XDG_CONFIG_HOME": str(self.xdg_config),
                "PATH": f"{self.fake_bin}:{env['PATH']}",
            }
        )
        return subprocess.run(
            ["bash", str(ROOT_DIR / "scripts/bin/rclipboard-setup")],
            input=input_text,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_installed_scripts_avoid_known_bash4_only_constructs(self) -> None:
        for path in INSTALLED_SOURCE_SCRIPTS:
            with self.subTest(script=path.name):
                text = path.read_text()
                self.assertNotRegex(text, r"\$\{[^}]+,,\}")
                self.assertNotIn("declare -A", text)
                self.assertNotIn("mapfile", text)

    def test_launcher_does_not_require_realpath(self) -> None:
        text = (ROOT_DIR / "scripts/bin/rclipboard-launcher").read_text()
        self.assertNotIn("realpath", text)
        self.assertIn("rclipboard-service-run", text)

    def test_launcher_delegates_to_canonical_xdg_runner(self) -> None:
        runner = self.app_dir / "bin/rclipboard-service-run"
        runner.parent.mkdir(parents=True)
        run_log = self.home / "runner.log"
        runner.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$0|$*\" > \"$RUN_LOG\"\n"
        )
        runner.chmod(0o755)

        result = subprocess.run(
            ["bash", str(ROOT_DIR / "scripts/bin/rclipboard-launcher")],
            env={
                **os.environ,
                "HOME": str(self.home),
                "XDG_CONFIG_HOME": str(self.xdg_config),
                "RUN_LOG": str(run_log),
            },
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(run_log.is_file(), result.stderr)
        self.assertEqual(run_log.read_text(), f"{runner}|\n")

    def test_launcher_daemonize_returns_while_runner_continues_once(self) -> None:
        runner = self.app_dir / "bin/rclipboard-service-run"
        runner.parent.mkdir(parents=True)
        run_log = self.home / "daemon.log"
        runner.write_text(
            "#!/bin/sh\n"
            "printf 'started\\n' >> \"$RUN_LOG\"\n"
            "sleep 3\n"
        )
        runner.chmod(0o755)

        started = time.monotonic()
        result = subprocess.run(
            [
                "bash",
                str(ROOT_DIR / "scripts/bin/rclipboard-launcher"),
                "daemonize",
            ],
            env={
                **os.environ,
                "HOME": str(self.home),
                "XDG_CONFIG_HOME": str(self.xdg_config),
                "RUN_LOG": str(run_log),
            },
            capture_output=True,
            text=True,
            timeout=1,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(time.monotonic() - started, 1)
        deadline = time.monotonic() + 1
        while not run_log.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(run_log.is_file(), result.stderr)
        self.assertEqual(run_log.read_text(), "started\n")

    def test_wizard_default_choice_writes_canonical_private_uds_config(
        self,
    ) -> None:
        result = self._run_wizard("\nn\nn\nn\nn\n")

        self.assertEqual(result.returncode, 0, result.stderr)
        config_path = self.app_dir / "config.toml"
        with config_path.open("rb") as file:
            config = tomllib.load(file)
        self.assertEqual(
            config["server"]["endpoint"],
            "uds://${XDG_RUNTIME_DIR}/rclipboard/uds.sock",
        )
        self.assertEqual(config["client"]["transport"], "uds")
        self.assertNotIn("fifo", config)
        self.assertIn(str(self.app_dir / "bin"), result.stdout)
        self.assertNotIn("~/.config/rclipboard", result.stdout)

    def test_wizard_tcp_choice_writes_fixed_loopback_fallback(self) -> None:
        result = self._run_wizard("2\n\n\nn\nn\nn\nn\n")

        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            with (self.app_dir / "config.toml").open("rb") as file:
                config = tomllib.load(file)
        except tomllib.TOMLDecodeError as error:
            self.fail(f"wizard wrote invalid TOML: {error}")
        self.assertEqual(config["server"]["endpoint"], "127.0.0.1:8989")
        self.assertEqual(config["client"]["transport"], "tcp")
        self.assertEqual(config["client"]["endpoint"], "127.0.0.1:8989")
        self.assertNotIn("fifo", config)
        self.assertIn("SSH", result.stdout)

    def test_wizard_tcp_choice_rejects_non_private_endpoint(self) -> None:
        for host, port in (("0.0.0.0", "8989"), ("127.0.0.1", "8990")):
            with self.subTest(host=host, port=port):
                (self.app_dir / "config.toml").unlink(missing_ok=True)
                result = self._run_wizard(f"2\n{host}\n{port}\n")
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.app_dir / "config.toml").exists())

    def test_wizard_darwin_does_not_enable_xsel_even_when_it_is_available(
        self,
    ) -> None:
        result = self._run_wizard(
            "\n\n\n\nn\nn\nn\nn\n",
            platform="Darwin",
            xsel_available=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        with (self.app_dir / "config.toml").open("rb") as file:
            config = tomllib.load(file)
        self.assertFalse(config["xsel"]["enabled"])
        self.assertIn("unavailable", result.stdout.lower())


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
        self.systemctl_log.unlink()
        result = _run_systemd_adapter(self.home, self.fake_bin, action="uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(unit.read_bytes(), original)
        log = self.systemctl_log.read_text()
        self.assertNotIn("disable --now rclipboard.service", log)
        self.assertIn("disable --now rclipboard-display.service", log)

    def test_linux_uninstall_does_not_disable_unmarked_display_unit(self) -> None:
        display = self.unit_dir / "rclipboard-display.service"
        original = b"[Unit]\nDescription=user-owned display\n"
        display.write_bytes(original)
        self.systemctl_log.unlink()

        result = _run_systemd_adapter(self.home, self.fake_bin, action="uninstall")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(display.read_bytes(), original)
        log = self.systemctl_log.read_text()
        self.assertNotIn("disable --now rclipboard-display.service", log)
        self.assertIn("disable --now rclipboard.service", log)

    def test_linux_uninstall_does_nothing_when_both_units_are_unmarked(self) -> None:
        main = self.unit_dir / "rclipboard.service"
        display = self.unit_dir / "rclipboard-display.service"
        main.write_bytes(b"user main\n")
        display.write_bytes(b"user display\n")
        self.systemctl_log.unlink()

        result = _run_systemd_adapter(self.home, self.fake_bin, action="uninstall")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.systemctl_log.exists())

    def test_linux_uninstall_propagates_main_disable_failure(self) -> None:
        display = self.unit_dir / "rclipboard-display.service"
        display.write_bytes(b"user-owned display\n")
        self.systemctl_log.unlink()

        result = _run_systemd_adapter(
            self.home,
            self.fake_bin,
            action="uninstall-conditional",
            disable_main_status=19,
            daemon_status=0,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.unit_dir / "rclipboard.service").exists())
        log = self.systemctl_log.read_text()
        self.assertIn("disable --now rclipboard.service", log)
        self.assertNotIn("daemon-reload", log)

    def test_linux_uninstall_propagates_marked_file_remove_failure(self) -> None:
        self.systemctl_log.unlink()
        display = self.unit_dir / "rclipboard-display.service"
        _make_failing_rm(self.fake_bin, display)
        result = _run_systemd_adapter(
            self.home,
            self.fake_bin,
            action="uninstall-conditional",
            daemon_status=0,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(display.exists())
        log = self.systemctl_log.read_text()
        self.assertIn("disable --now rclipboard-display.service", log)
        self.assertNotIn("daemon-reload", log)


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
            print_status=113,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "LaunchAgent installed; it will load at the next graphical login",
            result.stderr,
        )
        self.assertNotIn("bootstrap", self.launchctl_log.read_text())

    def test_macos_unexpected_gui_probe_failure_is_an_error(self) -> None:
        for action in ("start", "restart"):
            with self.subTest(action=action):
                self.launchctl_log.unlink(missing_ok=True)
                result = _run_launchd_adapter(
                    self.home,
                    self.fake_bin,
                    action=action,
                    print_status=5,
                )

                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("graphical login", result.stderr)
                log = self.launchctl_log.read_text()
                self.assertNotIn("bootstrap", log)
                self.assertNotIn("kickstart", log)

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
            action="uninstall-conditional",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.plist_path.exists())

        original = b"user-owned plist\n"
        self.plist_path.write_bytes(original)
        self.launchctl_log.unlink(missing_ok=True)
        result = _run_launchd_adapter(
            self.home,
            self.fake_bin,
            action="uninstall",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.plist_path.read_bytes(), original)
        self.assertFalse(self.launchctl_log.exists())


class LegacyEntryPointTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.home = self.tmp_path / "home"
        self.fake_bin = self.tmp_path / "fake-bin"
        self.entry_dir = self.tmp_path / "legacy-entry"
        self.home.mkdir()
        self.fake_bin.mkdir()
        self.entry_dir.mkdir()
        self.systemctl_log = _make_fake_systemctl(self.fake_bin)
        _make_fake_python(self.fake_bin)
        self.legacy_entry = self.entry_dir / "install-systemd-user.sh"
        shutil.copy2(
            ROOT_DIR / "scripts/install-systemd-user.sh",
            self.legacy_entry,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_positional_checkout_delegates_to_unified_installer(self) -> None:
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.fake_bin}:{env['PATH']}",
                "PYTHON_BIN": str(self.fake_bin / "python3"),
                "SYSTEMCTL_BIN": str(self.fake_bin / "systemctl"),
                "RCLIPBOARD_INSTALL_SKIP_PIP": "1",
                "RCLIPBOARD_INSTALL_UNAME": "Linux",
            }
        )

        result = subprocess.run(
            ["bash", str(self.legacy_entry), str(ROOT_DIR)],
            cwd=self.entry_dir,
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("deprecated", result.stderr.lower())
        app_dir = self.home / ".config/rclipboard"
        self.assertTrue((app_dir / "install.conf").exists())
        self.assertEqual(
            _read_metadata(app_dir / "install.conf")["repo_dir"],
            str(ROOT_DIR),
        )
        self.assertIn(
            "--user enable --now rclipboard.service",
            self.systemctl_log.read_text(),
        )

    def test_legacy_entry_point_contains_no_installation_implementation(self) -> None:
        source = (ROOT_DIR / "scripts/install-systemd-user.sh").read_text()
        self.assertNotIn(" -m venv ", source)
        self.assertNotIn("copy_executable", source)
        self.assertNotIn("service_install", source)
        self.assertNotIn("rclipboard.service", source)
        self.assertLessEqual(len(source.splitlines()), 10)


class InstallerBuildEntryPointTests(unittest.TestCase):
    @staticmethod
    def _recipe(makefile: str, target: str) -> list[str]:
        lines = makefile.splitlines()
        start = next(
            index + 1
            for index, line in enumerate(lines)
            if line.startswith(f"{target}:")
        )
        recipe: list[str] = []
        for line in lines[start:]:
            if line.startswith("\t"):
                recipe.append(line[1:])
            elif not line.strip():
                if recipe:
                    break
            else:
                break
        return recipe

    def test_make_exposes_platform_neutral_service_lifecycle_targets(self) -> None:
        makefile = (ROOT_DIR / "Makefile").read_text()
        self.assertIn(
            "USER_CONFIG_HOME := $(if $(XDG_CONFIG_HOME),$(XDG_CONFIG_HOME),$(HOME)/.config)",
            makefile,
        )
        self.assertEqual(self._recipe(makefile, "service-install"), ["./scripts/install.sh"])
        self.assertEqual(
            self._recipe(makefile, "service-update"),
            ["$(USER_CONFIG_HOME)/rclipboard/bin/rclipboard-update"],
        )
        self.assertEqual(
            self._recipe(makefile, "service-reset"),
            ["./scripts/install.sh --reset"],
        )
        self.assertEqual(
            self._recipe(makefile, "service-uninstall"),
            ["$(USER_CONFIG_HOME)/rclipboard/bin/rclipboard-uninstall"],
        )
        self.assertEqual(
            self._recipe(makefile, "systemd-user-install"),
            ['@echo "systemd-user-install is a compatibility alias for service-install"'],
        )
        self.assertIn("systemd-user-install: service-install", makefile)
        self.assertEqual(
            self._recipe(makefile, "install-no-systemd"),
            ["./scripts/install.sh --no-start"],
        )
        self.assertIn("install: .venv", makefile)

    def test_installer_dockerfiles_create_updateable_git_checkouts(self) -> None:
        for name in ("Dockerfile", "Dockerfile.systemd"):
            with self.subTest(name=name):
                source = (ROOT_DIR / "docker/test-install" / name).read_text()
                self.assertRegex(source, r"apt-get install[^\n]*(?:\\\n[^\n]*)*\bgit\b")
                self.assertIn("git init -b main", source)
                self.assertIn("git config user.name 'Installer Test'", source)
                self.assertIn("git config user.email 'installer@example.invalid'", source)
                self.assertIn("git add .", source)
                self.assertIn("git commit -m fixture", source)
                self.assertIn("git remote add origin .", source)

    def test_deploy_integration_fixture_provides_installer_git_prerequisite(self) -> None:
        source = (ROOT_DIR / "tests/docker/deploy/Dockerfile").read_text()
        self.assertRegex(source, r"apt-get install[^\n]*(?:\\\n[^\n]*)*\bgit\b")

    def test_real_systemd_harness_uses_unified_autostart_and_lifecycle_commands(self) -> None:
        source = (ROOT_DIR / "docker/test-install/systemd-test.sh").read_text()
        self.assertLess(
            source.index("chmod 0666 /dev/tty"),
            source.index("su - tester -c"),
        )
        self.assertIn("scripts/install.sh", source)
        self.assertNotIn("install-systemd-user.sh", source)
        self.assertNotIn("systemctl --user start rclipboard.service", source)
        self.assertIn("rclipboard-update", source)
        self.assertIn("rclipboard-uninstall", source)
        self.assertIn("--unix-socket", source)
        self.assertIn("systemctl --user stop rclipboard.service", source)


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

    def test_install_rejects_symlinked_managed_paths_before_mutation(self) -> None:
        for kind in ("app", "bin", "installer"):
            with self.subTest(kind=kind):
                case_root = self.tmp_path / f"install-symlink-{kind}"
                home = case_root / "home"
                fake_bin = case_root / "fake-bin"
                home.mkdir(parents=True)
                fake_bin.mkdir()
                _make_fake_systemctl(fake_bin)
                _make_fake_launchctl(fake_bin)
                _make_fake_python(fake_bin)
                app_dir = home / ".config/rclipboard"
                app_dir.parent.mkdir(parents=True)
                external = case_root / "external"
                external.mkdir(mode=0o755)
                marker = external / "keep.txt"
                marker.write_bytes(b"external\n")

                if kind == "app":
                    app_dir.symlink_to(external, target_is_directory=True)
                else:
                    app_dir.mkdir()
                    (app_dir / kind).symlink_to(external, target_is_directory=True)

                result = _run_installer(
                    home,
                    fake_bin,
                    args=["--no-start"],
                )

                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertIn("unsafe", result.stderr)
                self.assertEqual(marker.read_bytes(), b"external\n")
                self.assertEqual(stat.S_IMODE(external.stat().st_mode), 0o755)
                self.assertEqual(list(external.iterdir()), [marker])

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

    def test_new_darwin_config_enables_native_pasteboard(self) -> None:
        home = self.tmp_path / "darwin-pasteboard-home"
        home.mkdir()

        result = _run_installer(
            home,
            self.fake_bin,
            platform="Darwin",
            args=["--no-start"],
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        with (home / ".config/rclipboard/config.toml").open("rb") as file:
            config = tomllib.load(file)
        self.assertTrue(config["pasteboard"]["enabled"])

    def test_darwin_install_preserves_existing_pasteboard_choice(self) -> None:
        home = self.tmp_path / "darwin-existing-pasteboard-home"
        config_path = home / ".config/rclipboard/config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("[pasteboard]\nenabled = false\n")

        result = _run_installer(
            home,
            self.fake_bin,
            platform="Darwin",
            args=["--no-start"],
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            config_path.read_text(),
            "[pasteboard]\nenabled = false\n",
        )

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
        self.assertFalse(config["pasteboard"]["enabled"])
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
    def _run_common_cleanup(self, app_dir: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "bash",
                "-c",
                '. "$1"; APP_DIR="$2"; BIN_DIR="$APP_DIR/bin"; '
                'VENV_DIR="$APP_DIR/venv"; '
                'INSTALLER_DIR="$APP_DIR/installer"; '
                'METADATA_FILE="$APP_DIR/install.conf"; '
                "remove_managed_runtime",
                "cleanup-test",
                str(ROOT_DIR / "scripts/install/common.sh"),
                str(app_dir),
            ],
            env={**os.environ, "HOME": str(self.home)},
            capture_output=True,
            text=True,
        )

    def _write_cleanup_baseline(self, app_dir: Path) -> dict[Path, bytes]:
        sentinels = {
            app_dir / "venv/runtime-sentinel": b"runtime\n",
            app_dir / "bin/rclipctl": b"command\n",
            app_dir / "installer/install.sh": b"installer\n",
            app_dir / "install.conf": b"metadata\n",
        }
        for path, content in sentinels.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return sentinels

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
        self.systemctl_log.unlink()

        result = self.run_installed()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(service.read_bytes(), b"user-owned\n")
        log = self.systemctl_log.read_text()
        self.assertNotIn("disable --now rclipboard.service", log)
        self.assertIn("disable --now rclipboard-display.service", log)

    def test_linux_daemon_reload_failure_prevents_runtime_removal(self) -> None:
        runtime = self.app_dir / "venv/runtime-sentinel"
        runtime.write_bytes(b"runtime\n")

        result = self.run_installed(extra_env={"SYSTEMCTL_DAEMON_STATUS": "7"})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed to uninstall systemd service", result.stderr)
        self.assertEqual(runtime.read_bytes(), b"runtime\n")

    def test_linux_main_disable_failure_prevents_runtime_removal(self) -> None:
        display = self.home / ".config/systemd/user/rclipboard-display.service"
        display.write_bytes(b"user-owned display\n")
        runtime = self.app_dir / "venv/runtime-sentinel"
        runtime.write_bytes(b"runtime\n")

        result = self.run_installed(
            extra_env={
                "SYSTEMCTL_DISABLE_MAIN_STATUS": "19",
                "SYSTEMCTL_DAEMON_STATUS": "0",
            }
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Uninstalled rclipboard", result.stdout)
        self.assertEqual(runtime.read_bytes(), b"runtime\n")

    def test_linux_unit_remove_failure_prevents_runtime_removal(self) -> None:
        unit_dir = self.home / ".config/systemd/user"
        runtime = self.app_dir / "venv/runtime-sentinel"
        runtime.write_bytes(b"runtime\n")
        _make_failing_rm(
            self.fake_bin,
            unit_dir / "rclipboard-display.service",
        )
        result = self.run_installed(
            extra_env={"SYSTEMCTL_DAEMON_STATUS": "0"}
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Uninstalled rclipboard", result.stdout)
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

    def test_cleanup_rejects_symlinked_app_or_managed_directory_before_mutation(
        self,
    ) -> None:
        for kind in ("app", "bin", "installer"):
            with self.subTest(kind=kind):
                case_root = self.tmp_path / f"symlink-{kind}"
                app_dir = case_root / "config/rclipboard"
                external = case_root / "external"
                external.mkdir(parents=True)
                if kind == "app":
                    external_app = external / "target"
                    sentinels = self._write_cleanup_baseline(external_app)
                    app_dir.parent.mkdir(parents=True)
                    app_dir.symlink_to(external_app, target_is_directory=True)
                else:
                    sentinels = self._write_cleanup_baseline(app_dir)
                    managed = app_dir / kind
                    shutil.rmtree(managed)
                    sentinels = {
                        path: content
                        for path, content in sentinels.items()
                        if managed not in path.parents
                    }
                    external_managed = external / kind
                    external_managed.mkdir()
                    managed.symlink_to(external_managed, target_is_directory=True)
                    target_name = "rclipctl" if kind == "bin" else "install.sh"
                    target = external_managed / target_name
                    target.write_bytes(b"external\n")
                    sentinels[target] = b"external\n"

                result = self._run_common_cleanup(app_dir)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unsafe", result.stderr)
                for path, content in sentinels.items():
                    self.assertEqual(path.read_bytes(), content)

    def test_cleanup_rejects_symlinked_installer_subdirectories_before_mutation(
        self,
    ) -> None:
        cases = {
            "install": ("install", "common.sh"),
            "systemd": ("systemd", "user/rclipboard.service"),
            "systemd-user": ("systemd/user", "rclipboard.service"),
            "launchd": ("launchd", "com.rclipboard.service.plist.in"),
            "config": ("config", "rclipboard.conf.example"),
        }
        for name, (relative_dir, payload_name) in cases.items():
            with self.subTest(directory=name):
                case_root = self.tmp_path / f"installer-subdir-{name}"
                app_dir = case_root / "config/rclipboard"
                sentinels = self._write_cleanup_baseline(app_dir)
                managed_dir = app_dir / "installer" / relative_dir
                if managed_dir.exists():
                    shutil.rmtree(managed_dir)
                managed_dir.parent.mkdir(parents=True, exist_ok=True)
                external_dir = case_root / "external"
                external_dir.mkdir(parents=True)
                managed_dir.symlink_to(external_dir, target_is_directory=True)
                external_target = external_dir / payload_name
                external_target.parent.mkdir(parents=True, exist_ok=True)
                external_target.write_bytes(b"external payload\n")
                sentinels[external_target] = b"external payload\n"

                result = self._run_common_cleanup(app_dir)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unsafe", result.stderr)
                for path, content in sentinels.items():
                    self.assertEqual(path.read_bytes(), content)


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
        self.launchctl_log.unlink(missing_ok=True)

        result = self.run_installed()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(plist.read_bytes(), b"user-owned plist\n")
        self.assertFalse(self.launchctl_log.exists())


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
