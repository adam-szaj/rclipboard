from __future__ import annotations

import asyncio as a
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from .log import get_logger
from .main import qget

from messages import (
    makeResponse,
    makeResponseError,
    makeSystemResponse,
    make_broadcast_publish,
    normalize_data_items,
)
from . import xsel as xsel_mod
from . import proxy as proxy_mod

from .log import get_logger

def error(*args):
    get_logger(__name__).error(*args)

def warning(*args):
    get_logger(__name__).warning(*args)

def info(*args):
    get_logger(__name__).info(*args)

def debug(*args):
    get_logger(__name__).debug(*args)

def trace(*args):
    get_logger(__name__).trace(*args)

class Connection:
    def __init__(self, app: FastAPI, ws: WebSocket):
        self.app = app
        self.ws = ws
        self.q: a.Queue = a.Queue()
        self.topics: set[str] = set()

    async def send(self, payload: dict):
        await self.ws.send_json(payload)


async def dispatcher(app: FastAPI):
    bus: a.Queue = app.state.bus
    while True:
        item = await qget(app)
        info(f"item from bus: {item}")
        meta = item.get("meta", {})
        data_items = item.get("data_items", [])
        source = item.get("source")

        # Update content store and fan-out
        for di in data_items:

            info(f"di: {di}")
            topic = di.get("topic")
            if not topic:
                info(f"not topic continue")
                continue
            app.state.topic_content[topic] = {**di}
            subs = app.state.subs.get(topic, set())
            info(f"subs: {subs}")
            if subs:
                payload = make_broadcast_publish(di, meta=meta)
                info(f"payload: {payload}")
                for conn in list(subs):
                    if source is not None and conn is source:
                        continue
                    try:
                        conn.q.put_nowait(payload)
                    except a.QueueFull:
                        trace("ws client queue full; dropping oldest")
                        try:
                            conn.q.get_nowait()
                        except a.QueueEmpty:
                            trace("ws client queue empty while dropping oldest")
                        await conn.q.put(payload)
        # Also sync xsel and proxy for topics processed
        try:
            for di in data_items:
                await xsel_mod.on_topic_update(app, di)
        except Exception as e:
            debug(f"xsel sync error: {e}", exc_info=True)
        try:
            await proxy_mod.on_local_publish(app, data_items, meta, item.get("source"))
        except Exception as e:
            debug(f"proxy forward error: {e}", exc_info=True)
        bus.task_done()


def _subscribe(app: FastAPI, conn: Connection, topics: list[str]) -> list[str]:
    added: list[str] = []
    for t in topics:
        subs = app.state.subs.get(t, set())
        if conn not in subs:
            subs.add(conn)
            app.state.subs[t] = subs
            added.append(t)
    conn.topics.update(added)
    return added


def _unsubscribe(app: FastAPI, conn: Connection, topics: list[str]) -> list[str]:
    removed: list[str] = []
    for t in list(topics):
        subs = app.state.subs.get(t)
        if subs and conn in subs:
            subs.discard(conn)
            removed.append(t)
            if not subs:
                app.state.subs.pop(t, None)
    conn.topics.difference_update(removed)
    return removed


async def _worker(conn: Connection):
    try:
        while True:
            payload = await conn.q.get()
            await conn.send(payload)
    except Exception:
        pass


async def _handler(app: FastAPI, conn: Connection, msg: dict) -> dict | None:
    mtype = msg.get("type")
    if mtype == "system-request":
        action = msg.get("action")
        if action == "subscribe":
            topics = msg.get("topics", [])
            added = _subscribe(app, conn, topics)
            return makeSystemResponse(msg, event="subscribed", topics=added)
        if action == "unsubscribe":
            topics = msg.get("topics", [])
            removed = _unsubscribe(app, conn, topics)
            return makeSystemResponse(msg, event="unsubscribed", topics=removed)
        if action == "ping":
            return makeSystemResponse(msg, event="pong")
        return makeResponseError(msg, {"message": f"unknown system action: {action}"})

    if mtype == "request" and msg.get("action") == "call":
        method = msg.get("method")
        if method == "publish":
            meta = msg.get("meta", {})
            try:
                items = normalize_data_items(msg.get("params", {}).get("data"))
            except Exception as e:  # noqa: BLE001
                return makeResponseError(msg, {"message": str(e)})
            await app.state.bus.put({"source": conn, "meta": meta, "data_items": items})
            return makeResponse(msg, value={"published": len(items)})
        if method == "get":
            topic = msg.get("params", {}).get("topic")
            if not topic:
                return makeResponseError(msg, {"message": "params.topic is required"})
            value = app.state.topic_content.get(topic)
            if value is None:
                return makeResponseError(msg, {"message": f"topic '{topic}' not found"})
            return makeResponse(msg, value=value)
        return makeResponseError(msg, {"message": f"unknown method: {method}"})

    # Optional: pass-through for client events
    if mtype == "event":
        # Could be logged/forwarded if needed
        return None

    return makeResponseError(msg, {"message": f"unknown message type: {mtype}"})


def install_ws(app: FastAPI) -> None:
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        conn = Connection(app, ws)
        worker = a.create_task(_worker(conn))
        try:
            while True:
                data = await ws.receive_json()
                resp = await _handler(app, conn, data)
                if resp:
                    await conn.send(resp)
        except WebSocketDisconnect:
            pass
        finally:
            # Cleanup
            _unsubscribe(app, conn, list(conn.topics))
            worker.cancel()
            try:
                await worker
            except a.CancelledError:
                pass
