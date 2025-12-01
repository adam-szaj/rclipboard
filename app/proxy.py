from __future__ import annotations
from .app_state import enqueue_topic_data

import asyncio as a
import json
import os
from typing import Any

from fastapi import FastAPI
from websockets.asyncio.client import connect

from logging import Logger
from .log import get_logger

logger: Logger = get_logger(__name__)
error = logger.error
warning = logger.warning
info = logger.info
debug = logger.debug
trace = logger.debug

from messages import makeMessage, next_id

DEFAULT_TOPICS = ["c", "p", "s"]


class ProxyClient:

    def __init__(self,
                 app: FastAPI,
                 *,
                 url: str,
                 unix: bool = False,
                 path: str = "",
                 topics: list[str] | None = None):
        self.app = app
        self.url = url
        self.path = path
        self.unix = unix
        self.ws: Any = None  # ws connection
        self.topics = topics or list(DEFAULT_TOPICS)
        self.connected = False

    async def _send_json(self, payload: dict[str, str]) -> None:
        await self.ws.send(json.dumps(payload))

    async def _subscribe(self):
        msg = {
            "type": "system-request",
            "id": next_id(),
            "action": "subscribe",
            "topics": self.topics,
        }
        await self._send_json(msg)

    async def send_clip(self, items: list[dict], meta: dict | None = None):
        if not self.connected:
            return
        payload = {
            "type": "request",
            "id": next_id(),
            "action": "call",
            "method": "clip",
            "params": {
                "data": items
            },
            "meta": meta or {},
        }
        await self._send_json(payload)

    async def run(self):
        while True:
            try:
                kwargs = {}
                if self.unix:
                    kwargs = {"unix": self.unix, "path": self.path}
                async with connect(self.url, **kwargs) as ws:
                    self.ws = ws
                    self.connected = True
                    await self._subscribe()
                    async for raw in ws:
                        if isinstance(raw, bytes):
                            continue
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            trace("proxy: failed to decode JSON from upstream")
                            continue
                        # Expect upstream broadcasts
                        if msg.get("type") == "broadcast" and msg.get(
                                "action") == "clip":
                            data = msg.get("data")
                            items = data if isinstance(data, list) else [data]
                            data = {
                                "source": "proxy_upstream",
                                "meta": msg.get("meta", {}),
                                "data_items": items
                            }
                            await enqueue_topic_data(app, data)
            except Exception as e:
                debug(f"proxy upstream error: {e}", exc_info=True)
                self.connected = False
                await a.sleep(1.0)


def _make_ws_url() -> dict | None:
    if os.environ.get("RCLIPBOARD_PROXY_UDS"):
        # websockets does not natively support UDS URLs universally; user should run a TCP tunnel
        path = os.environ["RCLIPBOARD_PROXY_UDS"]
        # Attempt a common ws+unix style if supported by runtime; else return None
        return {"url": "ws://localhost/ws", "path": f"{path}", "unix": True}
    host = os.environ.get("RCLIPBOARD_PROXY_ADDR") or "127.0.0.1"
    port = int(os.environ.get("RCLIPBOARD_PROXY_PORT") or 8989)
    return {"url": f"ws://{host}:{port}/ws"}


def install_proxy(app: FastAPI) -> None:
    enabled = os.environ.get("RCLIPBOARD_PROXY", "0") in ("1", "true", "True")
    app.state.proxy_enabled = enabled
    app.state.proxy_task = None
    app.state.proxy_client: ProxyClient | None = None
    if not enabled:
        return
    connection_params = _make_ws_url()
    # if not url:
    #     debug("proxy disabled: invalid upstream configuration")
    #     return
    client = ProxyClient(app, **connection_params)
    app.state.proxy_client = client
    app.state.proxy_task = a.create_task(client.run(), name="proxy_upstream")


async def on_local_clip(app: FastAPI, data_items: list[dict], meta: dict,
                        source: Any) -> None:
    """
    Forward local publishes upstream when proxy is enabled,
    excluding upstream-originated ones.
    """
    info(f"data_items: {data_items} meta: {meta} source: {source}")
    if not getattr(app.state, "proxy_enabled", False):
        return
    if source == "proxy_upstream":
        return
    client: ProxyClient | None = getattr(app.state, "proxy_client", None)
    if not client or not client.connected:
        return
    try:
        await client.send_clip(data_items, meta=meta)
    except Exception as e:
        debug(f"proxy forward error: {e}", exc_info=True)
