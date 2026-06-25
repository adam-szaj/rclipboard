from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import ssl
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
    ssl_certfile: Path | None = None,
    ssl_keyfile: Path | None = None,
):
    scheme = "https" if ssl_certfile else "http"
    env = base_env(RCLIPBOARD_ENDPOINT=f"{scheme}://127.0.0.1:{port}")
    if proxy:
        env["RCLIPBOARD_PROXY"] = "1"
        env["RCLIPBOARD_UPSTREAM_ENDPOINT"] = f"127.0.0.1:{upstream_port or 0}"
    else:
        env["RCLIPBOARD_PROXY"] = "0"
    if ssl_certfile and ssl_keyfile:
        env["RCLIPBOARD_SSL_CERTFILE"] = str(ssl_certfile)
        env["RCLIPBOARD_SSL_KEYFILE"] = str(ssl_keyfile)
    if extra_env:
        env.update(extra_env)

    # Launch via the real entry point (rclipboard.__init__:main) — same path
    # systemd uses — so the _RclipboardServer subclass (shutdown-notice broadcast)
    # is exercised. Bind address / SSL come from RCLIPBOARD_* env, not CLI flags.
    cmd = [sys.executable, "-c", "from rclipboard import main; main()"]

    return subprocess.Popen(
        cmd,
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
        with exc:
            return exc.code, json.loads(exc.read().decode())


def post_json(url: str, payload: object, extra_headers: dict[str, str] | None = None) -> tuple[int, object]:
    headers = {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        with exc:
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


def gen_self_signed_cert(tmp_dir: Path) -> tuple[Path, Path]:
    """Generate a self-signed cert+key in tmp_dir using openssl. Returns (cert, key)."""
    cert = tmp_dir / "cert.pem"
    key = tmp_dir / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "1",
            "-subj", "/CN=127.0.0.1",
            "-addext", "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


def ssl_client_ctx(ca_cert: Path) -> ssl.SSLContext:
    """Return an SSLContext that trusts only the given CA certificate."""
    ctx = ssl.create_default_context(cafile=str(ca_cert))
    return ctx


def get_json_ssl(url: str, ctx: ssl.SSLContext) -> tuple[int, object]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, json.loads(exc.read().decode())


def post_json_ssl(url: str, payload: object, ctx: ssl.SSLContext) -> tuple[int, object]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, json.loads(exc.read().decode())


def wait_https_ready(port: int, ctx: ssl.SSLContext, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            status, body = get_json_ssl(f"https://127.0.0.1:{port}/v1/health.get", ctx)
            if status == 200 and isinstance(body, dict) and body.get("ok") is True:
                return
        except Exception as exc:  # pragma: no cover - helper
            last_error = exc
        time.sleep(0.1)
    raise RuntimeError(f"SSL server on port {port} did not start: {last_error}")


@contextlib.contextmanager
def running_ssl_server(
    *,
    port: int,
    certfile: Path,
    keyfile: Path,
    proxy: bool = False,
    upstream_port: int | None = None,
    extra_env: dict[str, str] | None = None,
):
    ctx = ssl_client_ctx(certfile)
    proc = start_server(
        port=port,
        proxy=proxy,
        upstream_port=upstream_port,
        extra_env=extra_env,
        ssl_certfile=certfile,
        ssl_keyfile=keyfile,
    )
    try:
        wait_https_ready(port, ctx)
        yield proc
    finally:
        stop_process(proc)
