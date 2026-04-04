"""Integration smoke-tests for the rcliptunel SSH tunnel script.

Tests all four endpoint-type combinations using ssh -R (reverse tunnel), with a
Docker container acting as the remote SSH host:

  1. TCP  local  →  TCP  remote
  2. UDS  local  →  TCP  remote
  3. TCP  local  →  UDS  remote
  4. UDS  local  →  UDS  remote

Requirements (tests skip gracefully if absent):
  - Docker daemon running
  - Docker image built:
      docker build -t rcliptunel-test:latest tests/docker/tunel/
  - OpenSSH client (ssh, ssh-keygen) in PATH
  - OpenSSH ≥ 6.7 on both client and server for stream-local (UDS) forwarding

Run:
    make test-tunel
    # or:
    PYTHONPATH=src .venv/bin/python -m unittest tests.test_integration_tunel -v
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
TUNEL_SCRIPT = ROOT_DIR / "scripts" / "bin" / "rcliptunel"
_VENV_PYTHON = ROOT_DIR / ".venv" / "bin" / "python"

TUNEL_TEST_IMAGE = os.environ.get("RCLIPBOARD_TUNEL_TEST_IMAGE",
                                  "rcliptunel-test:latest")

# ── skip guards ───────────────────────────────────────────────────────────────


def _cmd_ok(*args: str, timeout: int = 10) -> bool:
    try:
        subprocess.run(list(args),
                       capture_output=True,
                       check=True,
                       timeout=timeout)
        return True
    except Exception:
        return False


_SKIP_REASONS: list[str] = []
if not shutil.which("ssh"):
    _SKIP_REASONS.append("ssh not in PATH")
if not shutil.which("ssh-keygen"):
    _SKIP_REASONS.append("ssh-keygen not in PATH")
if not _cmd_ok("docker", "info"):
    _SKIP_REASONS.append("docker not available")
elif not _cmd_ok("docker", "image", "inspect", TUNEL_TEST_IMAGE):
    _SKIP_REASONS.append(
        f"Docker image '{TUNEL_TEST_IMAGE}' not found; "
        f"build it with: docker build -t {TUNEL_TEST_IMAGE} tests/docker/tunel/"
    )

_SKIP_ALL = bool(_SKIP_REASONS)
_SKIP_MSG = "; ".join(_SKIP_REASONS) if _SKIP_REASONS else ""

# ── low-level helpers ─────────────────────────────────────────────────────────


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _python_exe() -> str:
    return str(_VENV_PYTHON) if _VENV_PYTHON.exists() else sys.executable


def _server_env(**extra: str) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT_DIR / "src")
    env["RCLIPBOARD_XSEL"] = "0"
    env["RCLIPBOARD_LOG_LEVEL"] = "warning"
    env["RCLIPBOARD_PY_LOG_LEVEL"] = "WARNING"
    env["RCLIPBOARD_PROXY"] = "0"
    env.update(extra)
    return env


# ── rclipboard server launchers ───────────────────────────────────────────────


def _start_tcp_server(port: int) -> subprocess.Popen:
    proc = subprocess.Popen(
        [
            _python_exe(),
            "-m",
            "uvicorn",
            "rclipboard.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT_DIR,
        env=_server_env(RCLIPBOARD_ENDPOINT=f"127.0.0.1:{port}"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_tcp_ready(port)
    return proc


def _start_uds_server(sock_path: str) -> subprocess.Popen:
    proc = subprocess.Popen(
        [_python_exe(), "-c", "import rclipboard; rclipboard.main()"],
        cwd=ROOT_DIR,
        env=_server_env(RCLIPBOARD_ENDPOINT=f"uds://{sock_path}"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_uds_ready(sock_path)
    return proc


def _stop(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


# ── readiness polling ─────────────────────────────────────────────────────────


def _wait_tcp_ready(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/v1/health.get",
                    timeout=2) as resp:
                if json.loads(resp.read()).get("ok"):
                    return
        except Exception as exc:
            last_err = exc
        time.sleep(0.15)
    raise RuntimeError(f"rclipboard on TCP:{port} not ready: {last_err}")


def _wait_uds_ready(sock_path: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        if not Path(sock_path).exists():
            time.sleep(0.1)
            continue
        uds = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            uds.settimeout(2)
            uds.connect(sock_path)
            conn = http.client.HTTPConnection("localhost", timeout=2)
            conn.sock = uds
            conn.request("GET", "/v1/health.get")
            if json.loads(conn.getresponse().read()).get("ok"):
                return
        except Exception as exc:
            last_err = exc
        finally:
            uds.close()
        time.sleep(0.15)
    raise RuntimeError(f"rclipboard on UDS:{sock_path} not ready: {last_err}")


# ── clip put helpers ──────────────────────────────────────────────────────────

_CLIP_PUT_BODY = {
    "items": [{
        "topic": "c",
        "mime": "text/plain",
        "encoding": "utf-8",
        "value": "",  # filled per call
    }],
    "meta": {
        "app": "tunel-smoke"
    },
}


def _put_tcp(port: int, topic: str, value: str) -> None:
    body = {
        **_CLIP_PUT_BODY,
        "items": [{
            **_CLIP_PUT_BODY["items"][0], "topic": topic,
            "value": value
        }],
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/clip.put",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200, f"clip.put failed: {resp.read()}"


def _put_uds(sock_path: str, topic: str, value: str) -> None:
    body = {
        **_CLIP_PUT_BODY,
        "items": [{
            **_CLIP_PUT_BODY["items"][0], "topic": topic,
            "value": value
        }],
    }
    data = json.dumps(body).encode()
    uds = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        uds.settimeout(5)
        uds.connect(sock_path)
        conn = http.client.HTTPConnection("localhost", timeout=5)
        conn.sock = uds
        conn.request(
            "POST",
            "/v1/clip.put",
            body=data,
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        assert resp.status == 200, (
            f"clip.put via UDS failed ({resp.status}): {resp.read()}")
    finally:
        uds.close()


# ── Docker SSH container ──────────────────────────────────────────────────────


class _SshContainer:
    """Ephemeral Docker container running sshd with a generated key pair."""

    container_id: str = ""
    ssh_port: int = 0
    key_path: str = ""
    _tmpdir: str = ""

    def start(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="rclip_tunel_test_")
        key_path = os.path.join(self._tmpdir, "id_ed25519")
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", key_path, "-N", ""],
            check=True,
            capture_output=True,
        )
        self.key_path = key_path
        pub_key = Path(key_path + ".pub").read_text().strip()
        self.ssh_port = _free_port()
        result = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "-p",
                f"127.0.0.1:{self.ssh_port}:22",
                "-e",
                f"AUTHORIZED_KEY={pub_key}",
                TUNEL_TEST_IMAGE,
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

    def _wait_ssh(self, timeout: float = 60) -> None:
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
        raise RuntimeError("SSH container not ready in time")

    def _base_ssh_cmd(self) -> list[str]:
        return [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "ConnectTimeout=3",
            "-i",
            self.key_path,
            "-p",
            str(self.ssh_port),
            "root@127.0.0.1",
        ]

    def exec_cmd(self,
                 *cmd: str,
                 timeout: float = 10) -> subprocess.CompletedProcess:
        """Run a command inside the container via docker exec."""
        return subprocess.run(
            ["docker", "exec", self.container_id, *cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def tunel_extra_args(self) -> list[str]:
        """SSH extra args to pass after '--' to rcliptunel."""
        return [
            "-i",
            self.key_path,
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "IdentitiesOnly=yes",
        ]


# ── test class ────────────────────────────────────────────────────────────────


@unittest.skipIf(_SKIP_ALL, _SKIP_MSG)
class TunnelSmokeTests(unittest.TestCase):
    """
    Smoke-tests for all four rcliptunel socket-type combinations.

    A single Docker SSH container is shared across the four test methods;
    each test manages its own rclipboard server process and SSH tunnel.
    """

    container: _SshContainer

    @classmethod
    def setUpClass(cls) -> None:
        cls.container = _SshContainer()
        cls.container.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.container.stop()

    def setUp(self) -> None:
        self._procs: list[subprocess.Popen] = []
        self._tmpdir = tempfile.mkdtemp(prefix="rclip_tunel_")

    def tearDown(self) -> None:
        for proc in reversed(self._procs):
            _stop(proc)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    # ── private helpers ───────────────────────────────────────────────────────

    def _track(self, proc: subprocess.Popen) -> subprocess.Popen:
        self._procs.append(proc)
        return proc

    def _sock(self, name: str) -> str:
        """Return a short UDS path inside the per-test temp dir."""
        return os.path.join(self._tmpdir, name)

    def _start_tunnel(self, local_spec: str,
                      remote_spec: str) -> subprocess.Popen:
        cmd = [
            str(TUNEL_SCRIPT),
            "--local",
            local_spec,
            "--remote",
            remote_spec,
            "--ssh",
            "root@127.0.0.1",
            "--ssh-port",
            str(self.container.ssh_port),
            "--reverse",
            "--",
            *self.container.tunel_extra_args(),
        ]
        return self._track(
            subprocess.Popen(cmd,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL))

    def _wait_tunnel_tcp(self,
                         remote_port: int,
                         timeout: float = 20.0) -> None:
        """Poll until the tunneled TCP port responds inside the container."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r = self.container.exec_cmd(
                "curl",
                "-fsS",
                "--max-time",
                "2",
                f"http://127.0.0.1:{remote_port}/v1/health.get",
            )
            if r.returncode == 0:
                try:
                    if json.loads(r.stdout).get("ok"):
                        return
                except json.JSONDecodeError:
                    pass
            time.sleep(0.5)
        raise RuntimeError(
            f"Tunnel to TCP:{remote_port} not ready after {timeout}s")

    def _wait_tunnel_uds(self,
                         remote_sock: str,
                         timeout: float = 20.0) -> None:
        """Poll until the tunneled UDS socket responds inside the container."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r = self.container.exec_cmd(
                "curl",
                "-fsS",
                "--max-time",
                "2",
                "--unix-socket",
                remote_sock,
                "http://localhost/v1/health.get",
            )
            if r.returncode == 0:
                try:
                    if json.loads(r.stdout).get("ok"):
                        return
                except json.JSONDecodeError:
                    pass
            time.sleep(0.5)
        raise RuntimeError(
            f"Tunnel to UDS:{remote_sock} not ready after {timeout}s")

    def _get_via_tcp_tunnel(self, remote_port: int, topic: str) -> str:
        """clip.get via the tunneled TCP endpoint, executed inside the container."""
        r = self.container.exec_cmd(
            "curl",
            "-fsS",
            "--max-time",
            "5",
            "-X",
            "POST",
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps({"topic": topic}),
            f"http://127.0.0.1:{remote_port}/v1/clip.get",
        )
        self.assertEqual(r.returncode, 0,
                         f"GET via TCP tunnel failed: {r.stderr}")
        return json.loads(r.stdout)["item"]["value"]

    def _get_via_uds_tunnel(self, remote_sock: str, topic: str) -> str:
        """clip.get via the tunneled UDS endpoint, executed inside the container."""
        r = self.container.exec_cmd(
            "curl",
            "-fsS",
            "--max-time",
            "5",
            "--unix-socket",
            remote_sock,
            "-X",
            "POST",
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps({"topic": topic}),
            "http://localhost/v1/clip.get",
        )
        self.assertEqual(r.returncode, 0,
                         f"GET via UDS tunnel failed: {r.stderr}")
        return json.loads(r.stdout)["item"]["value"]

    # ── test cases ────────────────────────────────────────────────────────────

    def test_tcp_local_to_tcp_remote(self) -> None:
        """Case 1: local TCP server → container TCP port via ssh -R."""
        local_port = _free_port()
        remote_port = 19101

        self._track(_start_tcp_server(local_port))
        self._start_tunnel(
            f"tcp:127.0.0.1:{local_port}",
            f"tcp:127.0.0.1:{remote_port}",
        )
        self._wait_tunnel_tcp(remote_port)

        _put_tcp(local_port, "c", "hello-tcp-to-tcp")
        self.assertEqual(self._get_via_tcp_tunnel(remote_port, "c"),
                         "hello-tcp-to-tcp")

    def test_uds_local_to_tcp_remote(self) -> None:
        """Case 2: local UDS server → container TCP port via ssh -R (OpenSSH ≥ 6.7)."""
        sock_path = self._sock("srv.sock")
        remote_port = 19102

        self._track(_start_uds_server(sock_path))
        self._start_tunnel(
            f"uds://{sock_path}",
            f"tcp:127.0.0.1:{remote_port}",
        )
        self._wait_tunnel_tcp(remote_port)

        _put_uds(sock_path, "c", "hello-uds-to-tcp")
        self.assertEqual(self._get_via_tcp_tunnel(remote_port, "c"),
                         "hello-uds-to-tcp")

    def test_tcp_local_to_uds_remote(self) -> None:
        """Case 3: local TCP server → container UDS socket via ssh -R (OpenSSH ≥ 6.7)."""
        local_port = _free_port()
        remote_sock = "/tmp/rclip_tcp_uds.sock"

        self._track(_start_tcp_server(local_port))
        self._start_tunnel(
            f"tcp:127.0.0.1:{local_port}",
            f"uds://{remote_sock}",
        )
        self._wait_tunnel_uds(remote_sock)

        _put_tcp(local_port, "c", "hello-tcp-to-uds")
        self.assertEqual(self._get_via_uds_tunnel(remote_sock, "c"),
                         "hello-tcp-to-uds")

    def test_uds_local_to_uds_remote(self) -> None:
        """Case 4: local UDS server → container UDS socket via ssh -R (OpenSSH ≥ 6.7)."""
        sock_path = self._sock("srv.sock")
        remote_sock = "/tmp/rclip_uds_uds.sock"

        self._track(_start_uds_server(sock_path))
        self._start_tunnel(
            f"uds://{sock_path}",
            f"uds://{remote_sock}",
        )
        self._wait_tunnel_uds(remote_sock)

        _put_uds(sock_path, "c", "hello-uds-to-uds")
        self.assertEqual(self._get_via_uds_tunnel(remote_sock, "c"),
                         "hello-uds-to-uds")


if __name__ == "__main__":
    unittest.main()
