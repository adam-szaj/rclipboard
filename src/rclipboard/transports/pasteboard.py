from __future__ import annotations

import asyncio as a
import base64
import os
import sys
from logging import Logger
from pathlib import Path

from fastapi import FastAPI
from pydantic import JsonValue

from rclipboard.core.interfaces import BidirectionalInterface
from rclipboard.core.state import (
    enqueue_topic_data,
    register_client,
    subscribe_client,
)
from rclipboard.envutil import env_bool
from rclipboard.log import get_logger
from rclipboard.models.wire import TopicData, ValueData
from rclipboard.timeutil import utc_timestamp

logger: Logger = get_logger(__name__)
info = logger.info
debug = logger.debug

DEFAULT_PBCOPY_PATH = Path("/usr/bin/pbcopy")
DEFAULT_PBPASTE_PATH = Path("/usr/bin/pbpaste")
DEFAULT_POLL_INTERVAL_MS = 250
TOPIC = "c"


def _configured_pbcopy_path() -> Path:
    return Path(
        os.environ.get("RCLIPBOARD_PBCOPY_PATH", str(DEFAULT_PBCOPY_PATH))
    )


def _configured_pbpaste_path() -> Path:
    return Path(
        os.environ.get("RCLIPBOARD_PBPASTE_PATH", str(DEFAULT_PBPASTE_PATH))
    )


def _configured_interval_ms() -> int:
    return int(
        os.environ.get(
            "RCLIPBOARD_PASTEBOARD_INTERVAL_MS",
            str(DEFAULT_POLL_INTERVAL_MS),
        )
    )


def tools_available(
    pbcopy_path: Path | None = None,
    pbpaste_path: Path | None = None,
) -> bool:
    pbcopy_path = pbcopy_path or _configured_pbcopy_path()
    pbpaste_path = pbpaste_path or _configured_pbpaste_path()
    return (
        sys.platform == "darwin"
        and os.access(pbcopy_path, os.X_OK)
        and os.access(pbpaste_path, os.X_OK)
    )


async def _exec(
    executable: Path,
    *,
    input_data: bytes | None = None,
    timeout: float = 5.0,
) -> tuple[int, bytes, bytes]:
    command_env = os.environ.copy()
    if not command_env.get("LC_ALL") and not command_env.get("LC_CTYPE"):
        command_env["LC_CTYPE"] = "UTF-8"
    proc = await a.create_subprocess_exec(
        executable,
        stdin=a.subprocess.PIPE if input_data is not None else None,
        stdout=a.subprocess.PIPE,
        stderr=a.subprocess.PIPE,
        env=command_env,
    )
    try:
        stdout, stderr = await a.wait_for(
            proc.communicate(input=input_data), timeout=timeout
        )
        return (
            proc.returncode if proc.returncode is not None else -1,
            stdout or b"",
            stderr or b"",
        )
    except a.TimeoutError:
        try:
            proc.terminate()
            await a.wait_for(proc.wait(), timeout=0.5)
        except a.TimeoutError:
            proc.kill()
            await proc.wait()
        return 124, b"", b"timeout"


def _error_message(command: str, code: int, stderr: bytes) -> str:
    detail = stderr.decode(errors="replace").strip()
    return detail or f"{command} exited with status {code}"


def _item_bytes(item: TopicData) -> bytes:
    value = item.value
    if value.value_type == "binary":
        if value.value_encoding == "base64":
            return base64.b64decode(value.value)
        if value.value_encoding == "hex":
            return bytes.fromhex(value.value)
        return value.value.encode()
    return value.value.encode()


class PasteboardState:
    def __init__(self) -> None:
        self.applied: bytes = b""
        self.seen: bytes = b""
        self.applied_ts: str = ""
        self.seen_ts: str = ""
        self.poll_ts: str | None = None


class PasteboardInterface(BidirectionalInterface):
    monitor_kind = "pasteboard"

    def __init__(self, app: FastAPI):
        super().__init__()
        self.app = app
        self.pbcopy_path = _configured_pbcopy_path()
        self.pbpaste_path = _configured_pbpaste_path()
        self.interval_ms = _configured_interval_ms()
        self.enabled = env_bool("RCLIPBOARD_PASTEBOARD") and tools_available(
            self.pbcopy_path, self.pbpaste_path
        )
        self.state = PasteboardState()
        self.last_error: str | None = None
        self._lock = a.Lock()
        self.task: a.Task | None = None
        if self.enabled:
            info("start macOS pasteboard task")
            self.task = a.create_task(self.poller(), name="pasteboard_poller")

    def __repr__(self) -> str:
        return "pasteboard"

    @property
    def name(self) -> str:
        return "pasteboard"

    @property
    def good(self) -> bool:
        return bool(
            self.enabled and self.task is not None and not self.task.done()
        )

    def status(self) -> dict[str, JsonValue]:
        return {
            "enabled": self.enabled,
            "good": self.good,
            "pbcopy_path": str(self.pbcopy_path),
            "pbpaste_path": str(self.pbpaste_path),
            "interval_ms": self.interval_ms,
            "topics": [TOPIC],
            "last_error": self.last_error,
        }

    async def read_item(self) -> None:
        async with self._lock:
            self.state.poll_ts = utc_timestamp()
            code, current, stderr = await _exec(self.pbpaste_path, timeout=1)
            if code != 0:
                self.last_error = _error_message("pbpaste", code, stderr)
                return
            self.last_error = None

            if current == self.state.seen:
                return
            self.state.seen = current
            self.state.seen_ts = self.state.poll_ts
            if current == self.state.applied or not current:
                return

        await enqueue_topic_data(
            self.app,
            TopicData(
                topic=TOPIC,
                value=ValueData(
                    value=base64.b64encode(current).decode(),
                    type="binary",
                    encoding="base64",
                ),
                meta={"app": "pasteboard"},
            ),
            source=self,
        )

    async def send(self, data: TopicData) -> None:
        if data.topic != TOPIC:
            return
        data_bytes = _item_bytes(data)
        async with self._lock:
            code, _, stderr = await _exec(
                self.pbcopy_path, input_data=data_bytes, timeout=2.5
            )
            if code != 0:
                self.last_error = _error_message("pbcopy", code, stderr)
                return
            self.last_error = None
            timestamp = utc_timestamp()
            self.state.applied = data_bytes
            self.state.applied_ts = timestamp
            self.state.seen = data_bytes
            self.state.seen_ts = timestamp

    async def poller(self) -> None:
        interval = self.interval_ms / 1000.0
        while True:
            try:
                await a.sleep(interval)
                await self.read_item()
            except a.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                debug("pasteboard read failed: %s", exc, exc_info=True)

    async def shutdown(self) -> None:
        await self.stop_drainer()
        if self.task is None:
            return
        self.task.cancel()
        try:
            await self.task
        except a.CancelledError:
            pass
        self.task = None


def install_pasteboard(app: FastAPI) -> None:
    app.state.pasteboard = None
    if not env_bool("RCLIPBOARD_PASTEBOARD"):
        return
    conn = PasteboardInterface(app)
    app.state.pasteboard = conn
    if not conn.enabled:
        return
    register_client(app, conn)
    subscribe_client(app, conn, [TOPIC])


async def shutdown_pasteboard(app: FastAPI) -> None:
    conn: PasteboardInterface | None = getattr(app.state, "pasteboard", None)
    if conn is not None:
        await conn.shutdown()


def get_pasteboard_status(app: FastAPI) -> dict[str, JsonValue]:
    conn: PasteboardInterface | None = getattr(app.state, "pasteboard", None)
    if conn is not None:
        return conn.status()
    return {
        "enabled": False,
        "good": False,
        "pbcopy_path": str(_configured_pbcopy_path()),
        "pbpaste_path": str(_configured_pbpaste_path()),
        "interval_ms": _configured_interval_ms(),
        "topics": [TOPIC],
        "last_error": None,
    }
