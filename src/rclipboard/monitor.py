"""rclipboard live monitor — ncurses TUI.

Connects to /v1/monitor.stream (WebSocket) for live push, falls back to
polling /v1/monitor.snapshot when WebSocket is unavailable.

Usage (from rclipctl):
    rclipctl monitor tui [--endpoint http://host:port]
"""
from __future__ import annotations

import asyncio
import curses
import datetime
import http.client
import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field


# ── snapshot helpers ──────────────────────────────────────────────────────────

def _ago(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 2:
        return f"{seconds:.1f}s"
    if seconds < 120:
        return f"{int(seconds)}s"
    if seconds < 7200:
        return f"{int(seconds // 60)}m"
    return f"{int(seconds // 3600)}h"


def _size_str(size: int | None) -> str:
    if size is None:
        return "—"
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    return f"{size / 1024 / 1024:.1f}MB"


# ── state ─────────────────────────────────────────────────────────────────────

@dataclass
class MonitorState:
    ts_utc: str = ""
    clients: list[dict] = field(default_factory=list)
    topics: list[dict] = field(default_factory=list)
    proxy: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)   # ring buffer, max 40
    error: str | None = None
    paused: bool = False

    def apply_snapshot(self, snap: dict) -> None:
        self.ts_utc = snap.get("ts_utc", "")
        self.clients = snap.get("clients", [])
        self.topics = snap.get("topics", [])
        self.proxy = snap.get("proxy", {})

    def push_event(self, ev: dict) -> None:
        if self.paused:
            return
        self.events.append(ev)
        if len(self.events) > 40:
            self.events.pop(0)

    def apply_message(self, msg: dict) -> None:
        kind = msg.get("kind", "")
        if kind == "snapshot":
            self.apply_snapshot(msg.get("data", {}))
            return
        self.push_event(msg)
        # incrementally update clients/topics from events
        conn_id = msg.get("conn_id")
        topic = msg.get("topic")
        if kind == "client.connected":
            d = msg.get("data", {})
            self.clients.append({
                "conn_id": conn_id,
                "kind": d.get("kind", "?"),
                "addr": d.get("addr"),
                "app": None,
                "connected_ago": 0,
                "topics": [],
                "put_count": 0,
                "get_count": 0,
                "notify_count": 0,
            })
        elif kind == "client.disconnected":
            self.clients = [c for c in self.clients if c.get("conn_id") != conn_id]
        elif kind == "client.subscribed":
            topics = msg.get("data", {}).get("topics", [])
            for c in self.clients:
                if c.get("conn_id") == conn_id:
                    c["topics"] = sorted(set(c.get("topics", []) + topics))
        elif kind == "topic.put":
            d = msg.get("data", {})
            existing = next((t for t in self.topics if t.get("topic") == topic), None)
            if existing:
                existing["size"] = d.get("size")
                existing["stored_ago"] = 0
                existing["source_id"] = conn_id
                existing["source_app"] = d.get("app")
                existing["notify_count"] = 0
            else:
                self.topics.append({
                    "topic": topic,
                    "size": d.get("size"),
                    "stored_ago": 0,
                    "source_id": conn_id,
                    "source_app": d.get("app"),
                    "get_count": 0,
                    "notify_count": 0,
                })
            for c in self.clients:
                if c.get("conn_id") == conn_id:
                    c["put_count"] = c.get("put_count", 0) + 1
                    c["app"] = d.get("app") or c.get("app")
        elif kind == "topic.get":
            for t in self.topics:
                if t.get("topic") == topic:
                    t["get_count"] = t.get("get_count", 0) + 1
                    t["last_get_ago"] = 0
                    t["last_get_by"] = conn_id
            for c in self.clients:
                if c.get("conn_id") == conn_id:
                    c["get_count"] = c.get("get_count", 0) + 1
        elif kind == "topic.notify":
            for t in self.topics:
                if t.get("topic") == topic:
                    t["notify_count"] = t.get("notify_count", 0) + 1
            for c in self.clients:
                if c.get("conn_id") == conn_id:
                    c["notify_count"] = c.get("notify_count", 0) + 1


# ── curses renderer ───────────────────────────────────────────────────────────

_C_HEADER  = 1
_C_GREEN   = 2
_C_RED     = 3
_C_DIM     = 4
_C_BOLD    = 5
_C_YELLOW  = 6


def _init_colors() -> None:
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(_C_HEADER, curses.COLOR_CYAN,    -1)
    curses.init_pair(_C_GREEN,  curses.COLOR_GREEN,   -1)
    curses.init_pair(_C_RED,    curses.COLOR_RED,     -1)
    curses.init_pair(_C_DIM,    curses.COLOR_WHITE,   -1)
    curses.init_pair(_C_BOLD,   curses.COLOR_WHITE,   -1)
    curses.init_pair(_C_YELLOW, curses.COLOR_YELLOW,  -1)


def _trunc(s: str, n: int) -> str:
    if len(s) > n:
        return s[:n - 1] + "…"
    return s.ljust(n)


def _render(stdscr: "curses._CursesWindow", state: MonitorState,
            endpoint: str) -> None:
    stdscr.erase()
    max_y, max_x = stdscr.getmaxyx()
    row = 0

    def addstr(r: int, c: int, s: str, attr: int = 0) -> None:
        if r >= max_y or c >= max_x:
            return
        s = s[:max_x - c]
        try:
            stdscr.addstr(r, c, s, attr)
        except curses.error:
            pass

    now_str = datetime.datetime.now().strftime("%H:%M:%S")
    title = f" rclipboard monitor  {endpoint}  {now_str} "
    if state.paused:
        title += " [PAUSED]"
    addstr(row, 0, title[:max_x].ljust(max_x), curses.color_pair(_C_HEADER) | curses.A_BOLD)
    row += 1

    if state.error:
        addstr(row, 0, f"  ERROR: {state.error}", curses.color_pair(_C_RED))
        row += 1

    # ── TOPICS ────────────────────────────────────────────────────────────────
    row += 1
    addstr(row, 0, "  TOPICS", curses.color_pair(_C_HEADER))
    row += 1
    hdr = _trunc("topic", 10) + _trunc("size", 9) + _trunc("stored", 8) + \
          _trunc("source_app", 12) + _trunc("source_addr", 22) + \
          _trunc("gets", 6) + _trunc("notifies", 9)
    addstr(row, 2, hdr, curses.A_UNDERLINE)
    row += 1
    for t in sorted(state.topics, key=lambda x: x.get("topic", "")):
        if row >= max_y - 3:
            break
        line = _trunc(t.get("topic", "?"), 10) + \
               _trunc(_size_str(t.get("size")), 9) + \
               _trunc(_ago(t.get("stored_ago")), 8) + \
               _trunc(t.get("source_app") or "—", 12) + \
               _trunc(t.get("source_addr") or "—", 22) + \
               _trunc(str(t.get("get_count", 0)), 6) + \
               _trunc(str(t.get("notify_count", 0)), 9)
        addstr(row, 2, line)
        row += 1

    # ── CLIENTS ───────────────────────────────────────────────────────────────
    row += 1
    if row < max_y - 5:
        addstr(row, 0, "  CLIENTS", curses.color_pair(_C_HEADER))
        row += 1
        hdr2 = _trunc("conn_id", 28) + _trunc("kind", 7) + _trunc("app", 10) + \
               _trunc("topics", 12) + _trunc("puts", 6) + _trunc("gets", 6) + \
               _trunc("notifies", 9) + _trunc("ago", 7)
        addstr(row, 2, hdr2, curses.A_UNDERLINE)
        row += 1
        for c in state.clients:
            if row >= max_y - 3:
                break
            topics_str = ",".join(c.get("topics", [])) or "—"
            line = _trunc(c.get("conn_id", "?"), 28) + \
                   _trunc(c.get("kind", "?"), 7) + \
                   _trunc(c.get("app") or "—", 10) + \
                   _trunc(topics_str, 12) + \
                   _trunc(str(c.get("put_count", 0)), 6) + \
                   _trunc(str(c.get("get_count", 0)), 6) + \
                   _trunc(str(c.get("notify_count", 0)), 9) + \
                   _trunc(_ago(c.get("connected_ago")), 7)
            addstr(row, 2, line)
            row += 1

    # ── EVENTS ────────────────────────────────────────────────────────────────
    events_area = max_y - row - 2
    if events_area > 3:
        row += 1
        addstr(row, 0, "  EVENTS", curses.color_pair(_C_HEADER))
        row += 1
        for ev in state.events[-(events_area):]:
            if row >= max_y - 2:
                break
            ts = ev.get("ts_utc", "")[-8:] or "—"
            kind = _trunc(ev.get("kind", "?"), 22)
            topic = ev.get("topic") or ""
            conn = ev.get("conn_id") or ""
            data_s = json.dumps(ev.get("data", {}), separators=(",", ":"))
            line = f"{ts}  {kind} {_trunc(topic, 8)} {_trunc(conn, 24)} {data_s}"
            attr = curses.color_pair(_C_DIM)
            if "put" in ev.get("kind", ""):
                attr = curses.color_pair(_C_GREEN)
            elif "disconnect" in ev.get("kind", ""):
                attr = curses.color_pair(_C_RED)
            addstr(row, 2, line, attr)
            row += 1

    # ── footer ────────────────────────────────────────────────────────────────
    footer = " [q]uit  [p]ause/resume  [r]efresh "
    addstr(max_y - 1, 0, footer[:max_x].ljust(max_x),
           curses.color_pair(_C_HEADER))

    stdscr.refresh()


# ── polling client ────────────────────────────────────────────────────────────

def _fetch_snapshot(http_base: str, uds_path: str | None = None) -> dict | None:
    path = "/v1/monitor.snapshot"
    try:
        if uds_path:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(uds_path)
            conn = http.client.HTTPConnection("localhost")
            conn.sock = sock
            conn.request("GET", path)
            resp = conn.getresponse()
            data = resp.read().decode()
            conn.close()
            return json.loads(data)
        else:
            url = http_base.rstrip("/") + path
            with urllib.request.urlopen(url, timeout=5) as resp:
                return json.loads(resp.read().decode())
    except Exception:
        return None


# ── main TUI loop (polling fallback) ─────────────────────────────────────────

def run_tui_polling(http_base: str, uds_path: str | None = None,
                    interval: float = 2.0) -> None:
    state = MonitorState()
    label = uds_path or http_base

    def _main(stdscr: "curses._CursesWindow") -> None:
        _init_colors()
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(200)
        last_fetch = 0.0

        while True:
            now = time.monotonic()
            if now - last_fetch >= interval:
                snap = _fetch_snapshot(http_base, uds_path)
                if snap:
                    state.error = None
                    state.apply_snapshot(snap)
                else:
                    state.error = f"cannot reach {label}"
                last_fetch = now

            _render(stdscr, state, label)

            key = stdscr.getch()
            if key in (ord("q"), ord("Q"), 27):
                break
            elif key in (ord("p"), ord("P")):
                state.paused = not state.paused
            elif key in (ord("r"), ord("R")):
                last_fetch = 0.0

    curses.wrapper(_main)


# ── asyncio WS client ─────────────────────────────────────────────────────────

async def _run_ws_client(ws_url: str, state: MonitorState,
                         ready: asyncio.Event,
                         uds_path: str | None = None) -> None:
    try:
        from websockets.asyncio.client import connect  # type: ignore[import]
        kwargs: dict = {}
        if uds_path:
            kwargs["unix"] = True
            kwargs["path"] = uds_path
        async with connect(ws_url, **kwargs) as ws:
            ready.set()
            async for raw in ws:
                msg = json.loads(raw)
                state.apply_message(msg)
    except Exception as exc:
        state.error = str(exc)
        ready.set()


def run_tui_ws(ws_url: str, http_base: str, uds_path: str | None = None) -> None:
    state = MonitorState()
    label = uds_path or ws_url

    def _main(stdscr: "curses._CursesWindow") -> None:
        _init_colors()
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(50)

        loop = asyncio.new_event_loop()
        ws_task: asyncio.Task | None = None

        async def _ws_task(r: asyncio.Event) -> None:
            while True:
                await _run_ws_client(ws_url, state, r, uds_path)
                state.error = "reconnecting…"
                await asyncio.sleep(2)

        async def _start() -> asyncio.Task:
            r: asyncio.Event = asyncio.Event()
            t = asyncio.create_task(_ws_task(r))
            await asyncio.wait_for(r.wait(), timeout=5)
            return t

        ws_task = loop.run_until_complete(_start())

        try:
            while True:
                # sleep(0.05) gives the event loop 50 ms to drain all buffered
                # WS messages in one shot; sleep(0) only yields once, so a burst
                # of N events would take N×200 ms to appear.
                loop.run_until_complete(asyncio.sleep(0.05))
                _render(stdscr, state, label)
                key = stdscr.getch()
                if key in (ord("q"), ord("Q"), 27):
                    break
                elif key in (ord("p"), ord("P")):
                    state.paused = not state.paused
                elif key in (ord("r"), ord("R")):
                    snap = _fetch_snapshot(http_base, uds_path)
                    if snap:
                        state.apply_snapshot(snap)
        finally:
            if ws_task:
                ws_task.cancel()
            loop.run_until_complete(asyncio.sleep(0))
            loop.close()

    curses.wrapper(_main)


# ── entry point ───────────────────────────────────────────────────────────────

def main(http_base: str = "http://127.0.0.1:8989",
         uds_path: str | None = None) -> None:
    http_base = http_base.rstrip("/")
    if uds_path:
        # For UDS, use ws://localhost as the URI (host is ignored, path is used)
        ws_url = "ws://localhost/v1/monitor.stream"
    else:
        ws_url = http_base.replace("http://", "ws://").replace("https://", "wss://")
        ws_url += "/v1/monitor.stream"
    try:
        import websockets  # noqa: F401
        run_tui_ws(ws_url, http_base, uds_path)
    except ImportError:
        run_tui_polling(http_base, uds_path)
