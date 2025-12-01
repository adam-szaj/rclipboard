from __future__ import annotations

import asyncio as a
import base64
import os
from typing import Any
import shutil
import json
# from asyncio.timeouts import timeout
from .app_state import enqueue_topic_data, Connection, register_client, make_topic_data, subsctibe_client

from fastapi import FastAPI
from messages import utc_timestamp

from logging import Logger
from .log import get_logger
from pathlib import Path

logger: Logger = get_logger(__name__)
error = logger.error
warning = logger.warning
info = logger.info
debug = logger.debug
trace = logger.debug

XSEL_PATH: Path = Path(os.environ.get("RCLIPBOARD_XSEL_PATH", "/usr/bin/xsel"))
XSEL_ENABLED: bool = os.environ.get("RCLIPBOARD_XSEL",
                                    "1") not in ("0", "false", "False")
POLL_INTERVAL_MS: int = int(
    os.environ.get("RCLIPBOARD_XSEL_INTERVAL_MS", "500"))

# Topic to xsel option mapping
TOPIC_TO_XSEL = {
    "c": "-b",  # clipboard
    "p": "-p",  # primary
}


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _b64_decode(s: str) -> bytes:
    return base64.b64decode(s)


async def _exec(*args: str,
                input_data: bytes | None = None,
                timeout: float = 5.0) -> tuple[int, bytes, bytes]:
    """Run a process with optional stdin and a timeout.

    xsel -i may remain alive to own the selection on some setups; we therefore
    bound the wait and terminate on timeout to avoid hangs.
    """
    # Ensure X env is propagated
    env = os.environ.copy()
    proc = await a.create_subprocess_exec(
        *args,
        stdin=a.subprocess.PIPE if input_data is not None else None,
        stdout=None if input_data is not None else a.subprocess.PIPE,
        stderr=None if input_data is not None else a.subprocess.PIPE,
        env=env,
    )
    try:
        if input_data is None:
            stdout, stderr = await a.wait_for(proc.communicate(),
                                              timeout=timeout)
        else:
            stdout, stderr = await a.wait_for(
                proc.communicate(input=input_data), timeout=timeout)
        return proc.returncode, stdout or b"", stderr or b""
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
    code, out, err = await _exec(XSEL_PATH, opt, "-o", timeout=timeout)
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
        self.applied: bytes = b''
        self.seen: bytes = b''
        self.applied_ts: str | None = ''
        self.seen_ts: str | None = ''
        self.poll_ts: str | None = None


class XselConnection(Connection):

    def __init__(self, app: FastAPI):
        Connection.__init__(self)
        self.app = app
        self.enabled: bool = XSEL_ENABLED and os.path.exists(XSEL_PATH)
        self.selection_states: dict[str, XselState] = {}

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

        # Fire and forget health check
        # a.create_task(_health_check(app))

    def __repr__(self) -> str:
        return "'xsel'"

    def __str__(self) -> str:
        return "'xsel'"

    async def write_item(self, topic: str, item: dict[str, str]):
        opt = _selection_for_topic(topic)
        data = item.get("data", {}).get("data", {})
        info(f"topic: {topic} opt: {opt}")
        if not opt:
            return
        value: str | dict[str, str] = data.get("value", "")
        value_type: str = data.get("type", "binary")
        value_encoding: str = data.get("encoding", "base64")
        # compute bytes
        data_bytes = b''
        if value_type == "binary":
            if value_encoding == "base64":
                data_bytes = _b64_decode(value)
            elif value_encoding == "hex":
                data_bytes = bytes.fromhex(value)
        else:
            if isinstance(value, str):
                data_bytes = value.encode()
            else:
                data_bytes = json.dumps(value, separators=(",", ":")).encode()

        info(f"write_selection start: {data_bytes}")
        await write_selection(opt, data_bytes, timeout=2.5)
        info(f"write_selection done")

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

        topic_data = make_topic_data(source=self,
                                     topic=topic,
                                     value=_b64(current),
                                     type="binary",
                                     encoding="base64",
                                     app="xsel")
        await enqueue_topic_data(self.app, topic_data)

    async def poller(self):
        interval = POLL_INTERVAL_MS / 1000.0
        q: a.Queue = self.queue
        while True:
            try:
                item = await a.wait_for(q.get(), timeout=interval)
                info(f"xsel got item: {item}")
                # Drain any burst to reduce context switching
                q.task_done()
                topic = item.get("data", {}).get("data", {}).get("topic")
                info(f"write_item start")
                await self.write_item(topic, item)
                info(f"write_item done")

            except a.TimeoutError:
                # trace("xsel poller write wait timeout")
                pass
            except Exception as e:
                debug(f"xsel write error: {e}", exc_info=True)

            await self.read_items()

    async def enqueue_topic_data(self, data):
        info(f"enqueue_topic_data: {data}")
        topic = data.get("data", {}).get("data", {}).get("topic")
        info(f"topic: {topic}")
        assert topic
        if not _selection_for_topic(topic):
            return
        try:
            await self.queue.put(data)
        except a.QueueFull:
            trace("xsel queue full; dropping oldest")
            # drop oldest (best effort) and enqueue
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except a.QueueEmpty:
                trace("xsel queue empty while dropping oldest")

    async def send(self, data: dict[str, object]):
        pass


def install_xsel(app: FastAPI) -> None:
    conn = XselConnection(app)
    register_client(app, conn)
    subsctibe_client(app, conn, TOPIC_TO_XSEL.keys())


def _topic_for_selection(opt: str) -> str | None:
    rev = {v: k for k, v in TOPIC_TO_XSEL.items()}
    return rev.get(opt)


def _selection_for_topic(topic: str) -> str | None:
    return TOPIC_TO_XSEL.get(topic)


async def _health_check(app: FastAPI) -> None:
    """Populate xsel health info in self.xsel_health and warn if bad."""
    exists = os.path.exists(XSEL_PATH)
    executable = os.access(XSEL_PATH, os.X_OK)
    in_path = shutil.which(os.path.basename(XSEL_PATH)) is not None
    results: dict[str, Any] = {
        "path": XSEL_PATH,
        "exists": exists,
        "executable": executable,
        "in_path": in_path,
        "selections": {},
    }
    ok = exists and executable
    if exists and executable:
        for opt in ("-b", "-p", "-s"):
            code, _, _ = await _exec(XSEL_PATH, opt, "-o")
            results["selections"][opt] = {"read_ok": code == 0}
            ok = ok and (code == 0)
    results["ok"] = ok
    self.health = results
    if not ok:
        print(f"[xsel] health check failed: {results}")
