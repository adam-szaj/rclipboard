from __future__ import annotations

import asyncio as a
from asyncio.timeouts import timeout
import base64
import os
from typing import Any
import shutil

from fastapi import FastAPI
from .log import get_logger
from messages import utcTimestamp

XSEL_PATH = os.environ.get("RCLIPBOARD_XSEL_PATH", "/usr/bin/xsel")
XSEL_ENABLED = os.environ.get("RCLIPBOARD_XSEL",
                              "1") not in ("0", "false", "False")
POLL_INTERVAL_MS = int(os.environ.get("RCLIPBOARD_XSEL_INTERVAL_MS", "500"))

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


async def _exec(*args: str,
                input_data: bytes | None = None,
                timeout: float = 2.0) -> tuple[int, bytes, bytes]:
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
            # print(
            #     f"communicate timeout: {timeout} args: {args} input_data: {input_data}"
            # )
            stdout, stderr = await a.wait_for(
                proc.communicate(input=input_data), timeout=timeout)
        return proc.returncode, stdout or b"", stderr or b""
    except a.TimeoutError:
        get_logger(__name__).debug(f"xsel exec timeout: args={args}")
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
        get_logger(__name__).trace("xsel read_selection skipped: no DISPLAY")
        return b""
    code, out, err = await _exec(XSEL_PATH, opt, "-o", timeout=timeout)
    if code != 0:
        return b""
    return out


async def write_selection(opt: str, data: bytes, timeout: float) -> None:
    # If no DISPLAY, skip silently
    if not os.environ.get("DISPLAY"):
        get_logger(__name__).trace("xsel write_selection skipped: no DISPLAY")
        return
    # Place -i before selection flag is fine; keep order consistent
    await _exec(XSEL_PATH, "-n", "-i", opt, input_data=data, timeout=timeout)


def _topic_for_selection(opt: str) -> str | None:
    rev = {v: k for k, v in TOPIC_TO_XSEL.items()}
    return rev.get(opt)


def _selection_for_topic(topic: str) -> str | None:
    return TOPIC_TO_XSEL.get(topic)


async def on_topic_update(app: FastAPI, data_item: dict) -> None:
    """Enqueue DataItem for xsel application by the poller.

    The poller loop performs actual writes and bookkeeping to avoid feedback loops.
    """
    if not getattr(app.state, "xsel_enabled", False):
        return
    topic = data_item.get("topic")
    if not _selection_for_topic(topic):
        return
    try:
        # print(f"enqueue data_item:: {data_item}")
        app.state.xsel_queue.put_nowait(data_item)
    except a.QueueFull:
        get_logger(__name__).trace("xsel queue full; dropping oldest")
        # drop oldest (best effort) and enqueue
        try:
            app.state.xsel_queue.get_nowait()
            app.state.xsel_queue.task_done()
        except a.QueueEmpty:
            get_logger(__name__).trace(
                "xsel queue empty while dropping oldest")
        await app.state.xsel_queue.put(data_item)


async def poller(app: FastAPI):
    """Single loop handling outbound writes and periodic polling reads.

    - Waits with timeout for DataItems enqueued by on_topic_update(); writes to X.
    - On timeout, polls X selections and publishes user-originated changes.
    """
    interval = POLL_INTERVAL_MS / 1000.0
    q: a.Queue = app.state.xsel_queue
    while True:
        wrote = False
        try:
            # Wait for one item up to interval
            di = await a.wait_for(q.get(), timeout=interval)
            # print(f"got data from queue: {di}")
            # Drain any burst to reduce context switching
            batch = [di]
            # while True:
            #     try:
            #         qdi = q.get_nowait()
            #         batch.append(qdi)
            #     except a.QueueEmpty:
            #         break
            # Apply all queued writes
            # print(f"batch len: {len(batch)} di: {di}")
            for item in batch:
                # print(f"item from batch: {item}")
                topic = item.get("topic")
                opt = _selection_for_topic(topic)
                if not opt:
                    q.task_done()
                    continue
                value = item.get("value")
                value_type = item.get("valueType")
                value_encoding = item.get("valueEncoding")
                # compute bytes
                if value_type == "binary":
                    if value_encoding == "base64":
                        data_bytes = _b64_decode(value)
                    elif value_encoding == "hex":
                        data_bytes = bytes.fromhex(value)
                    else:
                        q.task_done()
                        continue
                else:
                    if isinstance(value, str):
                        data_bytes = value.encode()
                    else:
                        import json

                        data_bytes = json.dumps(value,
                                                separators=(",",
                                                            ":")).encode()
                await write_selection(opt, data_bytes, timeout=0.5)
                # remember last applied and seen
                ts = utcTimestamp()
                app.state.xsel_last_applied[opt] = data_bytes
                app.state.xsel_last_applied_ts[opt] = ts
                app.state.xsel_last_seen[opt] = data_bytes
                app.state.xsel_last_seen_ts[opt] = ts
                wrote = True
            q.task_done()
        except a.TimeoutError:
            get_logger(__name__).trace("xsel poller write wait timeout")
        except Exception as e:
            get_logger(__name__).debug(f"xsel write error: {e}", exc_info=True)

        # On timeout or after writes, consider polling
        try:
            app.state.xsel_last_poll_ts = utcTimestamp()
            for topic, opt in TOPIC_TO_XSEL.items():
                current = await read_selection(opt, timeout=0.5)
                last_applied = app.state.xsel_last_applied.get(opt)
                last_seen = app.state.xsel_last_seen.get(opt)
                if not current or current == last_seen:
                    continue
                else:
                    # print(f"current is not the same as the last one")
                    pass
                # update last seen immediately
                app.state.xsel_last_seen[opt] = current
                app.state.xsel_last_seen_ts[opt] = utcTimestamp()
                if last_applied is not None and current == last_applied:
                    # our own write: skip publish
                    continue
                di2 = {
                    "topic": topic,
                    "value": _b64(current),
                    "valueType": "binary",
                    "valueEncoding": "base64",
                }
                # print(f"poller di2: {di2}")
                await app.state.bus.put({
                    "source": None,
                    "meta": {
                        "app": "xsel"
                    },
                    "data_items": [di2]
                })
        except Exception as e:
            get_logger(__name__).debug(f"xsel read/publish error: {e}",
                                       exc_info=True)


def install_xsel(app: FastAPI) -> None:
    app.state.xsel_enabled = XSEL_ENABLED and os.path.exists(XSEL_PATH)
    app.state.xsel_last_applied: dict[str, bytes] = {}
    app.state.xsel_last_seen: dict[str, bytes] = {}
    app.state.xsel_last_applied_ts: dict[str, str] = {}
    app.state.xsel_last_seen_ts: dict[str, str] = {}
    app.state.xsel_last_poll_ts: str | None = None
    app.state.xsel_config = {
        "path": XSEL_PATH,
        "interval_ms": POLL_INTERVAL_MS,
    }
    app.state.xsel_health: dict | None = None
    app.state.xsel_queue: a.Queue = a.Queue(maxsize=32)
    if app.state.xsel_enabled:
        app.state.xsel_task = a.create_task(poller(app), name="xsel_poller")
    # Fire and forget health check
    a.create_task(_health_check(app))


async def _health_check(app: FastAPI) -> None:
    """Populate xsel health info in app.state.xsel_health and warn if bad."""
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
    app.state.xsel_health = results
    if not ok:
        print(f"[xsel] health check failed: {results}")
