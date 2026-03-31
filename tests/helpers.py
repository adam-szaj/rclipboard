from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def base_env(**extra: str) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT_DIR / "src")
    env["RCLIPBOARD_CONFIG"] = "/dev/null"  # disable user config.toml in tests
    env["RCLIPBOARD_XSEL"] = "0"
    env["RCLIPBOARD_LOG_LEVEL"] = "warning"
    env["RCLIPBOARD_PY_LOG_LEVEL"] = "WARNING"
    env.update(extra)
    return env


def start_server(
    *,
    port: int,
    proxy: bool = False,
    upstream_port: int | None = None,
    extra_env: dict[str, str] | None = None,
):
    env = base_env(RCLIPBOARD_ENDPOINT=f"127.0.0.1:{port}")
    if proxy:
        env["RCLIPBOARD_PROXY"] = "1"
        env["RCLIPBOARD_UPSTREAM_ENDPOINT"] = f"127.0.0.1:{upstream_port or 0}"
    else:
        env["RCLIPBOARD_PROXY"] = "0"
    if extra_env:
        env.update(extra_env)

    return subprocess.Popen(
        [
            sys.executable,
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
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def start_fifo_server(root: Path):
    env = base_env(RCLIPBOARD_ENDPOINT=f"fifo://{root}")
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import rclipboard; rclipboard.main()",
        ],
        cwd=ROOT_DIR,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def get_json(url: str) -> tuple[int, object]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def post_json(url: str, payload: object) -> tuple[int, object]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def wait_http_ready(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            status, body = get_json(f"http://127.0.0.1:{port}/v1/health.get")
            if status == 200 and isinstance(body, dict) and body.get("ok") is True:
                return
        except Exception as exc:  # pragma: no cover - helper
            last_error = exc
        time.sleep(0.1)
    raise RuntimeError(f"server on port {port} did not start: {last_error}")


def wait_for_value(
    port: int, topic: str, expected_value: object, timeout: float = 10.0
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last_body: object | None = None
    while time.monotonic() < deadline:
        status, body = post_json(
            f"http://127.0.0.1:{port}/v1/clip.get",
            {"topic": topic},
        )
        last_body = body
        if status == 200 and isinstance(body, dict):
            item = body.get("item")
            if isinstance(item, dict) and item.get("value") == expected_value:
                return body
        time.sleep(0.1)
    raise AssertionError(
        f"timed out waiting for topic={topic} value={expected_value!r} on port {port}, last={last_body!r}"
    )


def wait_for_path(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for path {path}")


def wait_for_file_bytes(path: Path, expected: bytes, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        if path.exists():
            last = path.read_bytes()
            if last == expected:
                return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path} == {expected!r}, last={last!r}")


@contextlib.contextmanager
def running_server(
    *,
    port: int,
    proxy: bool = False,
    upstream_port: int | None = None,
    extra_env: dict[str, str] | None = None,
):
    proc = start_server(
        port=port,
        proxy=proxy,
        upstream_port=upstream_port,
        extra_env=extra_env,
    )
    try:
        wait_http_ready(port)
        yield proc
    finally:
        stop_process(proc)


@contextlib.contextmanager
def running_fifo_server():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "fifo"
        root.mkdir()
        proc = start_fifo_server(root)
        try:
            wait_for_path(root / "health.json")
            yield root, proc
        finally:
            stop_process(proc)
