from __future__ import annotations

import asyncio as a
import contextlib
import json
import os
from logging import Logger
from typing import Any, override
from pathlib import Path

from pydantic import JsonValue
from fastapi import FastAPI
from websockets.asyncio.client import connect, unix_connect

from rclipboard.app_state import enqueue_topic_data
from rclipboard.helpers import (
    clipboard_item_to_topic_data,
    next_id,
    topic_data_to_clipboard_item,
    upstream_endpoint_from_env,
)
from rclipboard.log import get_logger
from rclipboard.types import ClipboardItem, TopicData, ClipWatchResult, BidirectionalInterface

logger: Logger = get_logger(__name__)
error = logger.error
warning = logger.warning
info = logger.info
debug = logger.debug
trace = logger.debug

DEFAULT_TOPICS = ["c", "p", "s"]


class ProxyClient(BidirectionalInterface):
    """
    Local proxy agent that maintains one upstream WS connection.

    Behavior:
    - on connect, subscribes upstream with `clip.watch`
    - on local publish, forwards `clip.put` upstream
    - on upstream `clip.changed`, writes the item into local AppState

    The remote programs then talk to the proxy's local server instead of the
    main server directly, which reduces cross-host chatter during copy/paste.
    """

    def __init__(
        self,
        app: FastAPI,
        *,
        url: str,
        unix: bool = False,
        path: str = "",
        topics: list[str] | set[str] | None = None,
    ):
        self.app = app
        self.url = url
        self.path = path
        self.unix = unix
        self.ws: Any = None
        self.topics = set(topics or DEFAULT_TOPICS)
        self.connected = False
        self._watch_id: int | str | None = None
        info(f"{self.__dict__}")

    @property
    @override
    def name(self) -> str:
        return "ProxyClient"

    @override
    async def send(self, data: TopicData):
        item = topic_data_to_clipboard_item(data)
        await self.send_clip(item, meta=data.meta)

    async def _send_json(self, payload: dict[str, JsonValue]) -> None:
        if self.ws is None:
            raise RuntimeError("proxy websocket is not connected")
        await self.ws.send(json.dumps(payload))

    async def _watch(self) -> None:
        self._watch_id = next_id()
        await self._send_json({
            "jsonrpc": "2.0",
            "id": self._watch_id,
            "method": "clip.watch",
            "params": {
                "topics": list(self.topics),
            },
        })

    async def send_clip(self, item: ClipboardItem, meta: dict[str, JsonValue] | None = None):
        if not self.connected:
            return
        await self._send_json({
            "jsonrpc": "2.0",
            "id": next_id(),
            "method": "clip.put",
            "params": {
                "items": [item.model_dump(mode="json")],
                "meta": meta or dict(),
            },
        })

    async def _handle_response(self, message: dict[str, JsonValue]) -> None:
        message_id = message.get("id")
        if message_id == self._watch_id:
            if "error" in message:
                warning(f"proxy watch rejected by upstream: {message['error']}")
            else:
                info(f"proxy subscribed upstream topics: {self.topics}")
                result = ClipWatchResult.model_validate(message.get("result"))
                for _, topic_data in result.contents.items():
                    await enqueue_topic_data(
                        self.app,
                        data=topic_data,
                        source=self,
                    )

    async def _handle_event(self, message: dict[str, object]) -> None:
        if message.get("method") != "clip.changed":
            return
        params = message.get("params")
        if not isinstance(params, dict):
            return
        raw_items = params.get("items")
        raw_meta = params.get("meta", {})
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        if not isinstance(raw_items, list):
            return
        for raw_item in raw_items:
            item = ClipboardItem.model_validate(raw_item)
            await enqueue_topic_data(
                self.app,
                data=_clipboard_item_to_topic_data(item, meta=meta),
                source=self,
            )

    async def run_loop(self):
        ws_cm: Any
        if self.unix:
            debug(f"unix_connect: path: {self.path} uri={self.url}")
            ws_cm = unix_connect(path=str(self.path), uri=self.url)
        else:
            ws_cm = connect(self.url)

        async with ws_cm as ws:
            self.ws = ws
            self.connected = True
            self.app.state.proxy_connected = True
            await self._watch()
            async for message in ws:
                if not isinstance(message, str):
                    continue
                try:
                    decoded = json.loads(message)
                except json.JSONDecodeError:
                    trace("proxy: failed to decode upstream JSON frame")
                    continue
                if not isinstance(decoded, dict):
                    continue
                if "method" in decoded:
                    await self._handle_event(decoded)
                elif "result" in decoded or "error" in decoded:
                    await self._handle_response(decoded)

    async def run(self):
        backoff = 1.0
        while True:
            try:
                await self.run_loop()
                backoff = 1.0
            except a.CancelledError:
                raise
            except Exception as exc:
                warning(f"proxy upstream error: {exc}", exc_info=True)
            finally:
                self.connected = False
                self.ws = None
                self.app.state.proxy_connected = False
            await a.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


def _make_ws_url() -> dict[str, str | bool | Path]:
    endpoint = upstream_endpoint_from_env()
    if endpoint.scheme == "uds":
        return {
            "url": "ws://localhost/ws",
            "path": endpoint.path,
            "unix": True,
        }
    if endpoint.scheme == "fifo":
        raise ValueError("fifo endpoints are not supported for proxy upstream connections")
    if endpoint.scheme in {"https", "wss"}:
        return {"url": f"wss://{endpoint.host}:{endpoint.port}/ws"}
    return {"url": f"ws://{endpoint.host}:{endpoint.port}/ws"}


def install_proxy(app: FastAPI) -> None:
    enabled = os.environ.get("RCLIPBOARD_PROXY", "0") in ("1", "true", "True")
    app.state.proxy_enabled = enabled
    app.state.proxy_connected = False
    app.state.proxy_task = None
    app.state.proxy_client = None
    if not enabled:
        return

    client = ProxyClient(app, **_make_ws_url())
    app.state.proxy_client = client
    app.state.local_topic_data_hooks.append(on_local_topic_data)
    app.state.proxy_task = a.create_task(client.run(), name="proxy_upstream")


async def shutdown_proxy(app: FastAPI) -> None:
    task = getattr(app.state, "proxy_task", None)
    if not task:
        return
    task.cancel()
    with contextlib.suppress(a.CancelledError):
        await task
    app.state.proxy_task = None
    app.state.proxy_connected = False


async def on_local_topic_data(app: FastAPI, topic_data: TopicData, source: Any) -> None:
    """
    Forward locally accepted clipboard updates to the upstream server.

    Events originating from this proxy are ignored to prevent loops:
    upstream `clip.changed` -> local store -> re-forward upstream.
    """
    info(f"on_local_topic_data: {topic_data}")
    if not getattr(app.state, "proxy_enabled", False):
        return
    client: ProxyClient | None = getattr(app.state, "proxy_client", None)
    if not client or not client.connected:
        return
    if source is client:
        return
    try:
        await client.send_clip(
            _topic_data_to_clipboard_item(topic_data),
            meta=topic_data.meta,
        )
    except Exception as exc:
        debug(f"proxy forward error: {exc}", exc_info=True)


_topic_data_to_clipboard_item = topic_data_to_clipboard_item
_clipboard_item_to_topic_data = clipboard_item_to_topic_data
