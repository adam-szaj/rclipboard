from __future__ import annotations

import asyncio as a
import base64
import os
from logging import Logger
from pathlib import Path

from fastapi import FastAPI

# from asyncio.timeouts import timeout
from rclipboard.app_state import (
    enqueue_topic_data,
    register_client,
    subscribe_client,
)
from rclipboard.helpers import utc_timestamp
from rclipboard.log import get_logger
from rclipboard.types import BidirectionalInterface, TopicData, ValueData

logger: Logger = get_logger(__name__)
error = logger.error
warning = logger.warning
info = logger.info
debug = logger.debug
trace = logger.debug

XSEL_PATH: Path = Path(os.environ.get("RCLIPBOARD_XSEL_PATH", "/usr/bin/xsel"))
XSEL_ENABLED: bool = os.environ.get("RCLIPBOARD_XSEL", "0") not in (
    "0",
    "false",
    "False",
)

POLL_INTERVAL_MS: int = int(
    os.environ.get("RCLIPBOARD_XSEL_INTERVAL_MS", "500")
)

# Topic to xsel option mapping
TOPIC_TO_XSEL = {
    "c": "-b",  # clipboard
    "p": "-p",  # primary
    "s": "-s",  # secondary
}


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _b64_decode(s: str) -> bytes:
    return base64.b64decode(s)


async def _exec(
    exec: Path,
    *args: str,
    input_data: bytes | None = None,
    timeout: float = 5.0,
) -> tuple[int, bytes, bytes]:
    """Run a process with optional stdin and a timeout.

    xsel -i may remain alive to own the selection on some setups; we therefore
    bound the wait and terminate on timeout to avoid hangs.
    """
    # Ensure X env is propagated
    env = os.environ.copy()
    proc = await a.create_subprocess_exec(
        exec,
        *args,
        stdin=a.subprocess.PIPE if input_data is not None else None,
        stdout=None if input_data is not None else a.subprocess.PIPE,
        stderr=None if input_data is not None else a.subprocess.PIPE,
        env=env,
    )
    try:
        if input_data is None:
            stdout, stderr = await a.wait_for(
                proc.communicate(), timeout=timeout
            )
        else:
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


async def read_selection(opt: str, timeout: float) -> bytes:
    # If no DISPLAY, xsel cannot work
    if not os.environ.get("DISPLAY"):
        trace("xsel read_selection skipped: no DISPLAY")
        return b""
    code, out, _ = await _exec(XSEL_PATH, opt, "-o", timeout=timeout)
    if code != 0:
        return b""
    return out


async def write_selection(opt: str, data: bytes, timeout: float) -> None:
    # If no DISPLAY, skip silently
    if not os.environ.get("DISPLAY"):
        trace("xsel write_selection skipped: no DISPLAY")
        return
    # Place -i before selection flag is fine; keep order consistent
    await _exec(XSEL_PATH, "-n", "-i", opt, input_data=data, timeout=timeout)


class XselState:
    def __init__(self, selection: str, opt: str):
        self.selection: str = selection
        self.opt: str = opt
        self.applied: bytes = b""
        self.seen: bytes = b""
        self.applied_ts: str = ""
        self.seen_ts: str = ""
        self.poll_ts: str | None = None


class XselInterface(BidirectionalInterface):
    def __init__(self, app: FastAPI):
        BidirectionalInterface.__init__(self)
        self.app = app
        self.enabled: bool = XSEL_ENABLED and os.path.exists(XSEL_PATH)
        self.selection_states: dict[str, XselState] = {}
        self.task: a.Task | None = None
        self.last_error: str | None = None

        for topic, opt in TOPIC_TO_XSEL.items():
            self.selection_states[topic] = XselState(topic, opt)

        self.config = {
            "path": XSEL_PATH,
            "interval_ms": POLL_INTERVAL_MS,
        }
        self.health: dict | None = None
        self.queue: a.Queue = a.Queue(maxsize=32)

        if self.enabled:
            info("start xsel task")
            self.task = a.create_task(self.poller(), name="xsel_poller")

    def __repr__(self) -> str:
        return "xsel"

    @property
    def name(self) -> str:
        return "xsel"

    @property
    def good(self) -> bool:
        return bool(self.enabled and os.environ.get("DISPLAY") and self.task)

    def status(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "good": self.good,
            "path": str(XSEL_PATH),
            "display": os.environ.get("DISPLAY", ""),
            "interval_ms": POLL_INTERVAL_MS,
            "topics": sorted(self.selection_states.keys()),
            "last_error": self.last_error,
        }

    async def write_item(self, topic: str, item: TopicData):
        opt = _selection_for_topic(topic)
        data = item.value
        info(f"topic: {topic} opt: {opt}")
        if not opt:
            return
        value: str = data.value
        value_type: str = data.value_type
        value_encoding: str = data.value_encoding
        # compute bytes
        data_bytes = b""
        if value_type == "binary":
            if value_encoding == "base64":
                data_bytes = _b64_decode(value)
            elif value_encoding == "hex":
                data_bytes = bytes.fromhex(value)
        else:
            assert isinstance(value, str)
            data_bytes = value.encode()

        await write_selection(opt, data_bytes, timeout=2.5)

        # remember last applied and seen
        ts = utc_timestamp()

        sel_state = self.selection_states[topic]

        sel_state.applied = data_bytes
        sel_state.applied_ts = ts
        sel_state.seen = data_bytes
        sel_state.seen_ts = ts

    async def read_items(self):
        for topic, state in self.selection_states.items():
            try:
                await self.read_item(topic, state)
            except Exception as e:
                self.last_error = str(e)
                debug(f"xsel read/clip error: {e}", exc_info=True)

    async def read_item(self, topic: str, state: XselState):
        # On timeout or after writes, consider polling
        state.poll_ts = utc_timestamp()
        opt = state.opt
        current = await read_selection(opt, timeout=1)

        if current == state.seen:
            return

        # update last seen immediately
        state.seen = current
        state.seen_ts = state.poll_ts
        if current == state.applied:
            return

        await enqueue_topic_data(
            self.app,
            TopicData(
                topic=topic,
                value=ValueData(
                    value=_b64(current), type="binary", encoding="base64"
                ),
                meta={"app": "xsel"},
            ),
            source=self,
        )

    async def poller(self):
        interval = POLL_INTERVAL_MS / 1000.0
        q: a.Queue = self.queue
        while True:
            try:
                item = await a.wait_for(q.get(), timeout=interval)
                info(f"xsel got item: {item}")
                # Drain any burst to reduce context switching
                q.task_done()
                topic = item.topic
                info("write_item start")
                await self.write_item(topic, item)
                info("write_item done")

            except a.TimeoutError:
                # trace("xsel poller write wait timeout")
                pass
            except Exception as e:
                self.last_error = str(e)
                debug(f"xsel write error: {e}", exc_info=True)

            await self.read_items()

    async def send(self, data: TopicData):
        try:
            self.queue.put_nowait(data)
        except a.QueueFull:
            self.last_error = "write queue full"
            warning("xsel write queue full; dropping update")

    async def shutdown(self) -> None:
        if not self.task:
            return
        while not self.queue.empty():
            try:
                item: TopicData = self.queue.get_nowait()
                self.queue.task_done()
                await self.write_item(item.topic, item)
            except Exception:
                break
        self.task.cancel()
        try:
            await self.task
        except a.CancelledError:
            pass
        self.task = None


def install_xsel(app: FastAPI) -> None:
    conn = XselInterface(app)
    app.state.xsel = conn
    register_client(app, conn)
    subscribe_client(app, conn, list(TOPIC_TO_XSEL.keys()))


async def shutdown_xsel(app: FastAPI) -> None:
    conn: XselInterface | None = getattr(app.state, "xsel", None)
    if conn:
        await conn.shutdown()


def get_xsel_status(app: FastAPI) -> dict[str, object]:
    conn: XselInterface | None = getattr(app.state, "xsel", None)
    if conn is None:
        return {
            "enabled": False,
            "good": False,
            "path": str(XSEL_PATH),
            "display": os.environ.get("DISPLAY", ""),
            "interval_ms": POLL_INTERVAL_MS,
            "topics": sorted(TOPIC_TO_XSEL.keys()),
            "last_error": None,
        }
    return conn.status()


def _selection_for_topic(topic: str) -> str | None:
    return TOPIC_TO_XSEL.get(topic)
