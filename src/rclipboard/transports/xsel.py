from __future__ import annotations

import asyncio as a
import base64
import os
from logging import Logger
from pathlib import Path

from fastapi import FastAPI
from pydantic import JsonValue

# from asyncio.timeouts import timeout
from rclipboard.core.state import (
    enqueue_topic_data,
    register_client,
    subscribe_client,
)
from rclipboard.core.interfaces import BidirectionalInterface
from rclipboard.log import get_logger
from rclipboard.models.wire import TopicData, ValueData
from rclipboard.timeutil import utc_timestamp

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
XSEL_ENCRYPT: bool = os.environ.get("RCLIPBOARD_XSEL_ENCRYPT", "0") not in (
    "0",
    "false",
    "False",
)
RCLIPCTL_PATH: Path = Path(os.environ.get("RCLIPCTL_PATH", "rclipctl"))

POLL_INTERVAL_MS: int = int(
    os.environ.get("RCLIPBOARD_XSEL_INTERVAL_MS", "500"))


def _default_display_env_file() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(runtime) / "rclipboard" / "display.env"


# Path to the file written by the session-bound rclipboard-display.service.
# It carries DISPLAY/WAYLAND_DISPLAY/XAUTHORITY for the *current* graphical
# session and is re-read on every xsel invocation, so the server process does
# not need to know about DISPLAY itself (and never has to restart to pick it
# up when the graphical session comes up after the service).
DISPLAY_ENV_FILE: Path = Path(
    os.environ.get("RCLIPBOARD_DISPLAY_ENV_FILE", "")
    or _default_display_env_file()
)

# Variables sourced from the display env-file and injected into xsel's env.
_DISPLAY_ENV_KEYS = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY")


def load_display_env(path: Path | None = None) -> dict[str, str]:
    """Read DISPLAY/WAYLAND_DISPLAY/XAUTHORITY from the display env-file.

    The file is written by the session-bound helper service in simple
    ``KEY=VALUE`` lines (optionally with surrounding quotes, as produced by
    ``systemd``/shell). Missing file or unreadable lines yield an empty dict
    so callers transparently fall back to the process environment.

    ``path`` defaults to the module-level :data:`DISPLAY_ENV_FILE`, resolved at
    call time so it honours patching/reconfiguration.
    """
    if path is None:
        path = DISPLAY_ENV_FILE
    result: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return result
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if key not in _DISPLAY_ENV_KEYS:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if value:
            result[key] = value
    return result


def _xsel_env() -> dict[str, str]:
    """Build the environment for xsel subprocesses.

    Starts from the process environment and overlays whatever the current
    graphical session published into the display env-file, so xsel always
    runs against the live DISPLAY even if the server started before login.
    """
    env = os.environ.copy()
    env.update(load_display_env())
    return env


def _display_available() -> bool:
    """True if a usable DISPLAY is known, from the env-file or the process env."""
    if load_display_env().get("DISPLAY"):
        return True
    return bool(os.environ.get("DISPLAY"))


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
    env: dict[str, str] | None = None,
) -> tuple[int, bytes, bytes]:
    """Run a process with optional stdin and a timeout.

    xsel -i may remain alive to own the selection on some setups; we therefore
    bound the wait and terminate on timeout to avoid hangs.
    """
    # Ensure X env is propagated; callers pass the display-aware env for xsel.
    if env is None:
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
            stdout, stderr = await a.wait_for(proc.communicate(),
                                              timeout=timeout)
        else:
            stdout, stderr = await a.wait_for(
                proc.communicate(input=input_data), timeout=timeout)
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
    # If no DISPLAY (from the display env-file or the process env), xsel
    # cannot work.
    if not _display_available():
        trace("xsel read_selection skipped: no DISPLAY")
        return b""
    env = _xsel_env()
    if XSEL_ENCRYPT:
        code, out, _ = await _exec(
            RCLIPCTL_PATH,
            "exec", "--encrypt-output", "--fetch-keys",
            "--", str(XSEL_PATH), opt, "-o",
            timeout=timeout,
            env=env,
        )
    else:
        code, out, _ = await _exec(XSEL_PATH, opt, "-o", timeout=timeout,
                                   env=env)
    if code != 0:
        return b""
    return out


async def write_selection(opt: str, data: bytes, timeout: float) -> None:
    # If no DISPLAY, skip silently
    if not _display_available():
        trace("xsel write_selection skipped: no DISPLAY")
        return
    # Place -i before selection flag is fine; keep order consistent
    await _exec(XSEL_PATH, "-n", "-i", opt, input_data=data, timeout=timeout,
                env=_xsel_env())


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

    # Monitoring classification (see Interface.monitor_kind).
    monitor_kind: str = "xsel"

    def __init__(self, app: FastAPI):
        BidirectionalInterface.__init__(self)
        self.app = app
        self.enabled: bool = XSEL_ENABLED and os.path.exists(XSEL_PATH)
        self.selection_states: dict[str, XselState] = {}
        self.task: a.Task | None = None
        self.last_error: str | None = None
        self._topic_locks: dict[str, a.Lock] = {
            topic: a.Lock() for topic in TOPIC_TO_XSEL
        }

        for topic, opt in TOPIC_TO_XSEL.items():
            self.selection_states[topic] = XselState(topic, opt)

        self.config = {
            "path": XSEL_PATH,
            "interval_ms": POLL_INTERVAL_MS,
        }
        self.health: dict | None = None

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
        return bool(self.enabled and _display_available() and self.task)

    def status(self) -> dict[str, JsonValue]:
        display = load_display_env().get("DISPLAY") or os.environ.get(
            "DISPLAY", "")
        return {
            "enabled": self.enabled,
            "good": self.good,
            "path": str(XSEL_PATH),
            "encrypt": XSEL_ENCRYPT,
            "display": display,
            "display_env_file": str(DISPLAY_ENV_FILE),
            "interval_ms": POLL_INTERVAL_MS,
            "topics": list(sorted(self.selection_states.keys())),
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
        data_bytes = b""
        if value_type == "binary":
            if value_encoding == "base64":
                data_bytes = _b64_decode(value)
            elif value_encoding == "hex":
                data_bytes = bytes.fromhex(value)
        else:
            assert isinstance(value, str)
            data_bytes = value.encode()

        async with self._topic_locks[topic]:
            ts = utc_timestamp()
            sel_state = self.selection_states[topic]
            sel_state.applied = data_bytes
            sel_state.applied_ts = ts
            sel_state.seen = data_bytes
            sel_state.seen_ts = ts

            encrypted = item.meta.get("encrypted", False) is True
            if XSEL_ENCRYPT and encrypted:
                await _exec(
                    RCLIPCTL_PATH,
                    "exec", "--decrypt-input",
                    "--", str(XSEL_PATH), "-n", "-i", opt,
                    input_data=data_bytes,
                    timeout=2.5,
                    env=_xsel_env(),
                )
            else:
                await write_selection(opt, data_bytes, timeout=2.5)

    async def read_items(self):
        for topic, state in self.selection_states.items():
            try:
                await self.read_item(topic, state)
            except Exception as e:
                self.last_error = str(e)
                debug(f"xsel read/clip error: {e}", exc_info=True)

    async def read_item(self, topic: str, state: XselState):
        state.poll_ts = utc_timestamp()
        opt = state.opt
        current = await read_selection(opt, timeout=1)

        async with self._topic_locks[topic]:
            if current == state.seen:
                return

            state.seen = current
            state.seen_ts = state.poll_ts
            if current == state.applied:
                return
            if not current:
                return

        meta: dict = {"app": "xsel"}
        if XSEL_ENCRYPT:
            meta["encrypted"] = True
        await enqueue_topic_data(
            self.app,
            TopicData(
                topic=topic,
                value=ValueData(value=_b64(current),
                                type="binary",
                                encoding="base64"),
                meta=meta,
            ),
            source=self,
        )

    async def poller(self):
        interval = POLL_INTERVAL_MS / 1000.0
        while True:
            try:
                await a.sleep(interval)
            except a.CancelledError:
                raise
            await self.read_items()

    async def send(self, data: TopicData) -> None:
        await self.write_item(data.topic, data)

    async def shutdown(self) -> None:
        if not self.task:
            return
        await self.stop_drainer()
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


def get_xsel_status(app: FastAPI) -> dict[str, JsonValue]:
    conn: XselInterface | None = getattr(app.state, "xsel", None)
    if conn is None:
        display = load_display_env().get("DISPLAY") or os.environ.get(
            "DISPLAY", "")
        return {
            "enabled": False,
            "good": False,
            "path": str(XSEL_PATH),
            "display": display,
            "display_env_file": str(DISPLAY_ENV_FILE),
            "interval_ms": POLL_INTERVAL_MS,
            "topics": list(sorted(TOPIC_TO_XSEL.keys())),
            "last_error": None,
        }
    return conn.status()


def _selection_for_topic(topic: str) -> str | None:
    return TOPIC_TO_XSEL.get(topic)
