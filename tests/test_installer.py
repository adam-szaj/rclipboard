"""Integration tests for scripts/install-systemd-user.sh.

Covers:
  - directory layout and installed scripts
  - config.toml generation from example
  - systemd unit file content (ExecStart, EnvironmentFile, WorkingDirectory)
  - WorkingDirectory override.conf for all three services
  - env file output from `rclipboard config env` (via Python interpreter)
  - env file is valid shell syntax
  - env file KEY="value" format
  - idempotency (second install does not overwrite config.toml or env)
"""
from __future__ import annotations

import os
import re
import shutil
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
    "rctrl-c",
    "rctrl-v",
    "rclip-smoke.sh",
    "rclipboard-tunel",
    "install-systemd-user.sh",
]

# Systemd service units that must be installed.
EXPECTED_UNITS = [
    "rclipboard.service",
    "rclipboard-proxy.service",
    "rclipboard@.service",
    "rclipboard.socket",
]

# Services that must have a WorkingDirectory override.conf.
OVERRIDE_SERVICES = [
    "rclipboard.service",
    "rclipboard-proxy.service",
    "rclipboard@.service",
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


def _run_installer(
    home: Path,
    fake_bin: Path,
    *,
    skip_pip: bool = True,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
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

    def test_units_exec_start_uses_venv_binary(self) -> None:
        expected = "ExecStart=%h/.config/rclipboard/venv/bin/rclipboard"
        for name in ["rclipboard.service", "rclipboard-proxy.service"]:
            with self.subTest(unit=name):
                self.assertIn(expected, (self.unit_dir / name).read_text())

    def test_fd_service_exec_start_has_fd_flag(self) -> None:
        text = (self.unit_dir / "rclipboard@.service").read_text()
        self.assertIn("ExecStart=%h/.config/rclipboard/venv/bin/rclipboard --fd 3", text)

    def test_main_service_has_exec_start_pre(self) -> None:
        text = (self.unit_dir / "rclipboard.service").read_text()
        self.assertIn("ExecStartPre=", text)
        self.assertIn("config env", text)

    def test_main_service_has_environment_file(self) -> None:
        text = (self.unit_dir / "rclipboard.service").read_text()
        self.assertIn("EnvironmentFile=%t/rclipboard/env", text)

    def test_working_directory_in_unit(self) -> None:
        text = (self.unit_dir / "rclipboard.service").read_text()
        self.assertIn("WorkingDirectory=%h/.config/rclipboard", text)

    # ── override.conf ─────────────────────────────────────────────────────────

    def test_override_conf_created_for_all_services(self) -> None:
        for svc in OVERRIDE_SERVICES:
            with self.subTest(service=svc):
                override = self.unit_dir / f"{svc}.d" / "override.conf"
                self.assertTrue(override.exists(), f"missing override for {svc}")
                self.assertIn(
                    f"WorkingDirectory={self.app_dir}",
                    override.read_text(),
                )

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
