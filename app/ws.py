from __future__ import annotations

import asyncio as a

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from messages import (makeResponse, makeResponseError, makeSystemResponse,
                      normalize_data_items)

from .app_state import Connection, enqueue_topic_data
from .log import get_logger as gl

logger = gl(__name__)
error = logger.error
warning = logger.warning
info = logger.info
debug = logger.debug
trace = logger.debug


class WSConnection(Connection):

    def __init__(self, app: FastAPI, ws: WebSocket):
        super().__init__()
        info(f"new connection app: {app}")
        self.app = app
        self.ws = ws
        self.q: a.Queue = a.Queue()
        self.topics: set[str] = set()

    async def send(self, payload: object):
        await self.ws.send_json(payload)


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


def _unsubscribe(app: FastAPI, conn: Connection,
                 topics: list[str]) -> list[str]:
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
            return makeSystemResponse(msg,
                                      event="unsubscribed",
                                      topics=removed)
        if action == "ping":
            return makeSystemResponse(msg, event="pong")
        return makeResponseError(
            msg, {"message": f"unknown system action: {action}"})

    if mtype == "request" and msg.get("action") == "call":
        method = msg.get("method")

        if method == "clip":
            meta = msg.get("meta", {})
            try:
                items = normalize_data_items(msg.get("params", {}).get("data"))
            except Exception as e:  # noqa: BLE001
                return makeResponseError(msg, {"message": str(e)})
            data = {"source": conn, "meta": meta, "data_items": items}
            await enqueue_topic_data(app, data)
            return makeResponse(msg, value={"clipped": len(items)})
        if method == "get":
            topic = msg.get("params", {}).get("topic")
            if not topic:
                return makeResponseError(
                    msg, {"message": "params.topic is required"})
            value = app.state.topic_content.get(topic)
            if value is None:
                return makeResponseError(
                    msg, {"message": f"topic '{topic}' not found"})
            return makeResponse(msg, value=value)
        return makeResponseError(msg, {"message": f"unknown method: {method}"})

    # Optional: pass-through for client events
    if mtype == "event":
        # Could be logged/forwarded if needed
        return None

    return makeResponseError(msg,
                             {"message": f"unknown message type: {mtype}"})


def install_ws(app: FastAPI) -> None:

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        conn = WSConnection(app, ws)
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
