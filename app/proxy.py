from __future__ import annotations

import asyncio as a
import json
import os
from typing import Any

from fastapi import FastAPI
from websockets.asyncio.client import connect
from .log import get_logger
from .main import qput, qget

from messages import makeMessage, next_id

DEFAULT_TOPICS = ["c", "p", "s"]

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


class ProxyClient:

    def __init__(self,
                 app: FastAPI,
                 url: str,
                 topics: list[str] | None = None):
        self.app = app
        self.url = url
        self.ws = None
        self.topics = topics or list(DEFAULT_TOPICS)
        self.connected = False

    async def _send_json(self, payload: dict) -> None:
        await self.ws.send(json.dumps(payload))

    async def _subscribe(self):
        msg = {
            "type": "system-request",
            "id": next_id(),
            "action": "subscribe",
            "topics": self.topics,
        }
        await self._send_json(msg)

    async def send_publish(self, items: list[dict], meta: dict | None = None):
        if not self.connected:
            warning("send publish: not connected")
            return
        payload = {
            "type": "request",
            "id": next_id(),
            "action": "call",
            "method": "publish",
            "params": {
                "data": items
            },
            "meta": meta or {},
        }
        info(f"sending: {payload}")
        await self._send_json(payload)

    async def run(self):
        while True:
            try:
                info(f"trying to connect to: {self.url}")
                async with connect(self.url) as ws:
                    info("connected to {self.url}: {ws}")
                    self.ws = ws
                    self.connected = True
                    await self._subscribe()
                    async for raw in ws:
                        info(f"raw: {raw}")
                        if isinstance(raw, bytes):
                            continue
                        try:
                            msg = json.loads(raw)
                            info(f"msg: {msg}")
                        except Exception:
                            error("proxy: failed to decode JSON from upstream")
                            continue
                        # Expect upstream broadcasts
                        if msg.get("type") == "broadcast" and msg.get(
                                "action") == "publish":
                            data = msg.get("data")
                            items = data if isinstance(data, list) else [data]
                            adata = {
                                "source":
                                "proxy_upstream",
                                "meta":
                                msg.get("meta", {}),
                                "data_items":
                                items
                            }
                            info("proxy put data: {adata}")
                            await qput(self.app, adata)
            except Exception as e:
                error(f"proxy upstream error: {e}", exc_info=True)
                self.connected = False
                await a.sleep(1.0)


def _make_ws_url() -> str | None:
    if os.environ.get("RCLIPBOARD_PROXY_UDS"):
        # websockets does not natively support UDS URLs universally; user should run a TCP tunnel
        path = os.environ["RCLIPBOARD_PROXY_UDS"]
        # Attempt a common ws+unix style if supported by runtime; else return None
        return f"ws+unix://{path}:/ws"
    host = os.environ.get("RCLIPBOARD_PROXY_ADDR") or "127.0.0.1"
    port = int(os.environ.get("RCLIPBOARD_PROXY_PORT") or 8989)
    return f"ws://{host}:{port}/ws"


def install_proxy(app: FastAPI) -> None:
    enabled = os.environ.get("RCLIPBOARD_PROXY", "0") in ("1", "true", "True")
    info(f"ENV: RCLIPBOARD_PROXY: {enabled}\nENV:\n{os.environ}")
    app.state.proxy_enabled = enabled
    app.state.proxy_task = None
    app.state.proxy_client: ProxyClient | None = None
    if not enabled:
        return
    url = _make_ws_url()
    if not url:
        warning("proxy disabled: invalid upstream configuration")
        return
    client = ProxyClient(app, url=url)
    app.state.proxy_client = client
    app.state.proxy_task = a.create_task(client.run(), name="proxy_upstream")


async def on_local_publish(app: FastAPI, data_items: list[dict], meta: dict,
                           source: Any) -> None:
    """
    Forward local publishes upstream when proxy is enabled,
    excluding upstream-originated ones.
    """
    info(f"on_local_publish:   {data_items}")
    if not getattr(app.state, "proxy_enabled", False):
        info("not proxy_enabled")
        return
    if source == "proxy_upstream":
        info(f"source: {source} == proxy_upstream")
        return
    client: ProxyClient | None = getattr(app.state, "proxy_client", None)
    if not client or not client.connected:
        info(f"not {client} or not {client.connected}")
        return
    try:
        info("send_publish: {data_items}")
        await client.send_publish(data_items, meta=meta)
    except Exception as e:
        debug(f"proxy forward error: {e}", exc_info=True)
