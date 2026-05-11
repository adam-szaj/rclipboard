"""Integration tests for scripts/deploy.sh, scripts/install.sh, and rclipboard-setup.

Four test classes:

  InstallScriptTests   — install.sh on a fresh container (no systemd)
  DeployScriptTests    — deploy.sh end-to-end: scripts, config, restart (none + systemd stubs)
  SetupWizardTests     — rclipboard-setup wizard non-interactively via stdin pipe

Requirements (Docker tests skip gracefully if absent):
  - Docker daemon running
  - Image built:  docker build -t rclipboard-deploy-test:latest tests/docker/deploy/
  - OpenSSH client (ssh, ssh-keygen) and rsync in PATH

SetupWizardTests requires only bash (no Docker).

Run:
    make test-deploy
    # or:
    PYTHONPATH=src .venv/bin/python -m unittest tests.test_integration_deploy -v
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
import tomllib
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DEPLOY_TEST_IMAGE = os.environ.get(
    "RCLIPBOARD_DEPLOY_TEST_IMAGE", "rclipboard-deploy-test:latest"
)


# ── skip guards ───────────────────────────────────────────────────────────────


def _cmd_ok(*args: str, timeout: int = 10) -> bool:
    try:
        subprocess.run(list(args), capture_output=True, check=True, timeout=timeout)
        return True
    except Exception:
        return False


_DOCKER_SKIP_REASONS: list[str] = []
if not shutil.which("ssh"):
    _DOCKER_SKIP_REASONS.append("ssh not in PATH")
if not shutil.which("ssh-keygen"):
    _DOCKER_SKIP_REASONS.append("ssh-keygen not in PATH")
if not shutil.which("rsync"):
    _DOCKER_SKIP_REASONS.append("rsync not in PATH")
if not _cmd_ok("docker", "info"):
    _DOCKER_SKIP_REASONS.append("docker daemon not available")
elif not _cmd_ok("docker", "image", "inspect", DEPLOY_TEST_IMAGE):
    _DOCKER_SKIP_REASONS.append(
        f"Docker image '{DEPLOY_TEST_IMAGE}' not found; "
        f"build it with: docker build -t {DEPLOY_TEST_IMAGE} tests/docker/deploy/"
    )

_SKIP_DOCKER = bool(_DOCKER_SKIP_REASONS)
_SKIP_DOCKER_MSG = "; ".join(_DOCKER_SKIP_REASONS) if _DOCKER_SKIP_REASONS else ""


# ── helpers ───────────────────────────────────────────────────────────────────


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _write_inventory(
    path: Path,
    host: str,
    init: str,
    eptype: str,
    extra: str = "",
) -> None:
    line = f"{host}  {init}  {eptype}"
    if extra:
        line += f"  {extra}"
    path.write_text(line + "\n")


def _run_deploy(
    *,
    inventory: Path,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess:
    cmd = [
        str(ROOT_DIR / "scripts" / "deploy.sh"),
        "--inventory",
        str(inventory),
    ] + (extra_args or [])
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
        env=os.environ.copy(),
        timeout=120,
    )


# ── container lifecycle ───────────────────────────────────────────────────────


class _DeployContainer:
    """Manages a Docker container running sshd for deploy/install testing."""

    def __init__(self) -> None:
        self.container_id: str = ""
        self.ssh_port: int = 0
        self.key_path: str = ""
        self._tmpdir: str = ""

    def start(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="rclip_deploy_test_")
        key = os.path.join(self._tmpdir, "id_ed25519")
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", key, "-N", ""],
            capture_output=True,
            check=True,
        )
        self.key_path = key
        pub_key = Path(key + ".pub").read_text().strip()
        self.ssh_port = _free_port()
        result = subprocess.run(
            [
                "docker", "run", "-d", "--rm",
                "-p", f"127.0.0.1:{self.ssh_port}:22",
                "-e", f"AUTHORIZED_KEY={pub_key}",
                DEPLOY_TEST_IMAGE,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.container_id = result.stdout.strip()
        self._wait_ssh(timeout=30)

    def stop(self) -> None:
        if self.container_id:
            subprocess.run(
                ["docker", "stop", self.container_id],
                capture_output=True,
                timeout=15,
            )
            self.container_id = ""
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = ""

    def _wait_ssh(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                r = subprocess.run(
                    self._base_ssh_cmd() + ["true"],
                    capture_output=True,
                    timeout=10,
                )
                if r.returncode == 0:
                    return
            except subprocess.TimeoutExpired:
                pass
            time.sleep(1)
        raise RuntimeError(f"SSH container on port {self.ssh_port} not ready in time")

    def _base_ssh_cmd(self) -> list[str]:
        return [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "IdentitiesOnly=yes",
            "-o", "ControlMaster=no",
            "-o", f"Port={self.ssh_port}",
            "-i", self.key_path,
            "root@127.0.0.1",
        ]

    def exec_cmd(
        self,
        *cmd: str,
        check: bool = False,
        timeout: float = 30.0,
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["docker", "exec", self.container_id, *cmd],
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout,
        )

    @property
    def extra_ssh_opts_str(self) -> str:
        """Single string for inventory 4th column; eval'd into an array by deploy.sh.

        Uses '-o Port=N' (not '-p N') so the same opts work for both ssh and scp.
        '-o ControlMaster=no' overrides deploy.sh's ControlMaster=auto to prevent
        stale mux sockets between test runs.
        """
        return (
            f"-o Port={self.ssh_port} "
            f"-i {self.key_path} "
            f"-o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null "
            f"-o IdentitiesOnly=yes "
            f"-o ControlMaster=no"
        )


# ── InstallScriptTests ────────────────────────────────────────────────────────


@unittest.skipIf(_SKIP_DOCKER, _SKIP_DOCKER_MSG)
class InstallScriptTests(unittest.TestCase):
    """scripts/install.sh on a fresh container (no systemd)."""

    container: _DeployContainer

    @classmethod
    def setUpClass(cls) -> None:
        cls.container = _DeployContainer()
        cls.container.start()
        c = cls.container
        ssh_e = (
            f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            f"-o IdentitiesOnly=yes -o ControlMaster=no "
            f"-o Port={c.ssh_port} -i {c.key_path}"
        )
        # Copy repo to container
        subprocess.run(
            [
                "rsync", "-az",
                "--exclude=.venv",
                "--exclude=__pycache__",
                "--exclude=*.pyc",
                "--exclude=.git",
                "-e", ssh_e,
                str(ROOT_DIR) + "/",
                "root@127.0.0.1:/tmp/rclipboard-src/",
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        # Run install.sh (skip pip to avoid internet dependency)
        subprocess.run(
            c._base_ssh_cmd() + [
                "env", "RCLIPBOARD_INSTALL_SKIP_PIP=1",
                "bash", "/tmp/rclipboard-src/scripts/install.sh",
                "/tmp/rclipboard-src",
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.container.stop()

    def test_install_bin_dir_exists(self) -> None:
        r = self.container.exec_cmd("test", "-d", "/root/.config/rclipboard/bin")
        self.assertEqual(r.returncode, 0)

    def test_install_venv_dir_exists(self) -> None:
        r = self.container.exec_cmd("test", "-d", "/root/.config/rclipboard/venv")
        self.assertEqual(r.returncode, 0)

    def test_install_rclipctl_in_user_bin(self) -> None:
        r = self.container.exec_cmd("test", "-x", "/root/bin/rclipctl")
        self.assertEqual(r.returncode, 0)

    def test_install_rcliptunel_in_user_bin(self) -> None:
        r = self.container.exec_cmd("test", "-x", "/root/bin/rcliptunel")
        self.assertEqual(r.returncode, 0)

    def test_install_rclipboard_setup_in_config_bin(self) -> None:
        r = self.container.exec_cmd(
            "test", "-f", "/root/.config/rclipboard/bin/rclipboard-setup"
        )
        self.assertEqual(r.returncode, 0)

    def test_install_creates_config_toml(self) -> None:
        r = self.container.exec_cmd("test", "-f", "/root/.config/rclipboard/config.toml")
        self.assertEqual(r.returncode, 0)

    def test_install_config_is_valid_toml(self) -> None:
        r = self.container.exec_cmd("cat", "/root/.config/rclipboard/config.toml")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = tomllib.loads(r.stdout)
        self.assertIn("server", data)
        self.assertIn("proxy", data)
        self.assertIn("endpoint", data["server"])

    def test_install_idempotent_preserves_config(self) -> None:
        c = self.container
        c.exec_cmd(
            "sh", "-c",
            "printf '# sentinel\\n' > /root/.config/rclipboard/config.toml",
            check=True,
        )
        subprocess.run(
            c._base_ssh_cmd() + [
                "env", "RCLIPBOARD_INSTALL_SKIP_PIP=1",
                "bash", "/tmp/rclipboard-src/scripts/install.sh",
                "/tmp/rclipboard-src",
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        r = c.exec_cmd("cat", "/root/.config/rclipboard/config.toml")
        self.assertEqual(r.stdout.strip(), "# sentinel")


# ── DeployScriptTests ─────────────────────────────────────────────────────────


@unittest.skipIf(_SKIP_DOCKER, _SKIP_DOCKER_MSG)
class DeployScriptTests(unittest.TestCase):
    """deploy.sh end-to-end against a Docker container."""

    container: _DeployContainer
    _class_tmpdir: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.container = _DeployContainer()
        cls.container.start()
        c = cls.container
        # Minimal pre-setup: directory structure + venv (no pip install needed)
        c.exec_cmd("mkdir", "-p", "/root/bin", "/root/.config/rclipboard/bin", check=True)
        c.exec_cmd("python3", "-m", "venv", "/root/.config/rclipboard/venv", check=True)
        cls._class_tmpdir = tempfile.mkdtemp(prefix="rclip_deploy_inv_")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.container.stop()
        shutil.rmtree(cls._class_tmpdir, ignore_errors=True)

    def setUp(self) -> None:
        fd, path = tempfile.mkstemp(dir=self._class_tmpdir, suffix=".inv")
        os.close(fd)
        self._inv = Path(path)

    def tearDown(self) -> None:
        self._inv.unlink(missing_ok=True)

    def _inv_none_tcp(self) -> None:
        _write_inventory(
            self._inv,
            "root@127.0.0.1",
            "none",
            "tcp",
            self.container.extra_ssh_opts_str,
        )

    def test_deploy_scripts_synced(self) -> None:
        self._inv_none_tcp()
        r = _run_deploy(
            inventory=self._inv,
            extra_args=["--skip-install", "--skip-config", "--skip-restart"],
        )
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(
            self.container.exec_cmd("test", "-x", "/root/bin/rclipctl").returncode, 0
        )
        self.assertEqual(
            self.container.exec_cmd("test", "-x", "/root/bin/rcliptunel").returncode, 0
        )
        self.assertEqual(
            self.container.exec_cmd(
                "test", "-f", "/root/.config/rclipboard/bin/rclipboard-setup"
            ).returncode,
            0,
        )

    def test_deploy_config_synced_tcp(self) -> None:
        self._inv_none_tcp()
        r = _run_deploy(
            inventory=self._inv,
            extra_args=["--skip-install", "--skip-restart"],
        )
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        cat = self.container.exec_cmd("cat", "/root/.config/rclipboard/config.toml")
        self.assertEqual(cat.returncode, 0, cat.stderr)
        data = tomllib.loads(cat.stdout)
        self.assertIn("server", data)
        self.assertEqual(data["client"]["transport"], "tcp")
        self.assertIn("127.0.0.1:8989", data["server"]["endpoint"])

    def test_deploy_config_synced_uds(self) -> None:
        _write_inventory(
            self._inv,
            "root@127.0.0.1",
            "none",
            "uds",
            self.container.extra_ssh_opts_str,
        )
        r = _run_deploy(
            inventory=self._inv,
            extra_args=["--skip-install", "--skip-restart"],
        )
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        cat = self.container.exec_cmd("cat", "/root/.config/rclipboard/config.toml")
        self.assertEqual(cat.returncode, 0, cat.stderr)
        data = tomllib.loads(cat.stdout)
        self.assertEqual(data["client"]["transport"], "uds")
        self.assertIn("uds://", data["server"]["endpoint"])

    def test_deploy_restart_none(self) -> None:
        self._inv_none_tcp()
        r = _run_deploy(
            inventory=self._inv,
            extra_args=["--only-restart"],
        )
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("restarting (none)", r.stdout)

    def test_deploy_restart_systemd(self) -> None:
        # Inject fake systemctl that logs its arguments
        self.container.exec_cmd(
            "sh", "-c",
            r"printf '#!/bin/sh\necho \"$*\" >> /tmp/systemctl.log\n'"
            r" > /usr/local/bin/systemctl"
            r" && chmod +x /usr/local/bin/systemctl",
            check=True,
        )
        try:
            _write_inventory(
                self._inv,
                "root@127.0.0.1",
                "systemd",
                "tcp",
                self.container.extra_ssh_opts_str,
            )
            r = _run_deploy(
                inventory=self._inv,
                extra_args=["--only-restart"],
            )
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
            log = self.container.exec_cmd("cat", "/tmp/systemctl.log")
            self.assertEqual(log.returncode, 0, "systemctl.log not found")
            self.assertIn("--user restart rclipboard.service", log.stdout)
        finally:
            self.container.exec_cmd(
                "rm", "-f", "/usr/local/bin/systemctl", "/tmp/systemctl.log"
            )

    def test_deploy_dry_run_no_changes(self) -> None:
        # Capture config state before dry-run
        pre = self.container.exec_cmd("cat", "/root/.config/rclipboard/config.toml")
        pre_content = pre.stdout if pre.returncode == 0 else ""

        self._inv_none_tcp()
        r = _run_deploy(
            inventory=self._inv,
            extra_args=["--dry-run"],
        )
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("[DRY-RUN]", r.stdout)

        # Config must be unchanged
        post = self.container.exec_cmd("cat", "/root/.config/rclipboard/config.toml")
        post_content = post.stdout if post.returncode == 0 else ""
        self.assertEqual(pre_content, post_content)

    def test_deploy_unreachable_host_skipped(self) -> None:
        closed_port = _free_port()  # port released immediately — unlikely open
        self._inv.write_text(
            f"root@127.0.0.1  none  tcp  {self.container.extra_ssh_opts_str}\n"
            f"unreachable-host  none  tcp"
            f"  -o HostName=127.0.0.1 -o Port={closed_port}"
            f" -i {self.container.key_path}"
            f" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
            f" -o IdentitiesOnly=yes -o ControlMaster=no\n"
        )
        r = _run_deploy(
            inventory=self._inv,
            extra_args=["--skip-install", "--skip-config", "--skip-restart"],
        )
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAILED", r.stdout)
        self.assertIn("unreachable-host", r.stdout)


# ── SetupWizardTests ──────────────────────────────────────────────────────────


_SKIP_WIZARD = not shutil.which("bash")
_SKIP_WIZARD_MSG = "bash not in PATH"


@unittest.skipIf(_SKIP_WIZARD, _SKIP_WIZARD_MSG)
class SetupWizardTests(unittest.TestCase):
    """rclipboard-setup wizard non-interactively via stdin pipe (no Docker needed)."""

    WIZARD = ROOT_DIR / "scripts" / "bin" / "rclipboard-setup"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="rclip_wizard_test_")
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @property
    def config_toml(self) -> Path:
        return self.home / ".config" / "rclipboard" / "config.toml"

    def _run_wizard(self, stdin_input: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["XDG_CONFIG_HOME"] = str(self.home / ".config")
        return subprocess.run(
            ["bash", str(self.WIZARD)],
            input=stdin_input,
            capture_output=True,
            text=True,
            env=env,
            cwd=ROOT_DIR,
            timeout=15,
        )

    def test_wizard_uds_creates_valid_toml(self) -> None:
        # 1=UDS, n=xsel, n=age, n=proxy, n=admin  (5 lines, no overwrite prompt)
        r = self._run_wizard("1\nn\nn\nn\nn\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.config_toml.exists())
        data = tomllib.loads(self.config_toml.read_text())
        self.assertIn("uds://", data["server"]["endpoint"])
        self.assertEqual(data["client"]["transport"], "uds")

    def test_wizard_tcp_creates_valid_toml(self) -> None:
        # 2=TCP, default host (enter), default port (enter), n=xsel, n=age, n=proxy, n=admin
        r = self._run_wizard("2\n\n\nn\nn\nn\nn\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.config_toml.exists())
        data = tomllib.loads(self.config_toml.read_text())
        self.assertIn("127.0.0.1:8989", data["server"]["endpoint"])
        self.assertEqual(data["client"]["transport"], "tcp")

    def test_wizard_all_sections_present(self) -> None:
        r = self._run_wizard("1\nn\nn\nn\nn\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = tomllib.loads(self.config_toml.read_text())
        for section in ["server", "fifo", "xsel", "proxy", "ssl", "client"]:
            with self.subTest(section=section):
                self.assertIn(section, data)

    def test_wizard_overwrite_guard_default_n(self) -> None:
        self.config_toml.parent.mkdir(parents=True, exist_ok=True)
        self.config_toml.write_text("# sentinel\n")
        # 6 lines: extra N at end for overwrite prompt
        r = self._run_wizard("1\nn\nn\nn\nn\nN\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.config_toml.read_text(), "# sentinel\n")

    def test_wizard_overwrite_accepted(self) -> None:
        self.config_toml.parent.mkdir(parents=True, exist_ok=True)
        self.config_toml.write_text("# sentinel\n")
        # y = accept overwrite
        r = self._run_wizard("1\nn\nn\nn\nn\ny\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = tomllib.loads(self.config_toml.read_text())
        self.assertIn("server", data)


if __name__ == "__main__":
    unittest.main()
