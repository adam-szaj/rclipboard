from __future__ import annotations

import asyncio as a
import contextlib
import datetime
import json
import os
import socket
import ssl
from logging import Logger
from pathlib import Path
from typing import Any, override

from fastapi import FastAPI
from pydantic import JsonValue
from websockets.asyncio.client import connect, unix_connect

from rclipboard.core.state import (
    enqueue_request_topic,
    enqueue_topic_data,
    enqueue_topic_data_nowait,
    register_client,
    subscribe_client,
    unregister_client,
    unsubscribe_client,
)
from rclipboard.endpoints import upstream_endpoint_from_env
from rclipboard.log import get_logger
from rclipboard.models.convert import (
    clipboard_item_to_topic_data,
    stub_topic_data,
    topic_data_to_clipboard_item,
)
from rclipboard.models.rpc import JSONRPCRequestMessage, RPCId, next_id
from rclipboard.models.wire import (
    ClipboardItem,
    ClipGetResult,
    ClipWatchResult,
    RPCError,
    TopicData,
)
from rclipboard.timeutil import parse_utc_timestamp, utc_timestamp
from rclipboard.transports.rpc_handler import RPCHandler, RPCMethodError

logger: Logger = get_logger(__name__)
error = logger.error
warning = logger.warning
info = logger.info
debug = logger.debug
trace = logger.debug

DEFAULT_TOPICS = ["c", "p", "s"]

_topic_data_to_clipboard_item = topic_data_to_clipboard_item
_clipboard_item_to_topic_data = clipboard_item_to_topic_data


class ProxyClient(RPCHandler):
    """
    Local proxy agent that maintains one upstream WS connection.

    Behavior:
    - on connect, subscribes upstream with `clip.watch`
    - on local publish, forwards `clip.put` upstream
    - on upstream `clip.changed`, writes the item into local AppState
    - handles incoming JSON-RPC requests from upstream (symmetric peer)

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
        RPCHandler.__init__(self, app)
        self.url = url
        self.path = path
        self.unix = unix
        self.ws: Any = None
        self.topics = set(topics or DEFAULT_TOPICS)
        self.connected = False
        self._watch_id: int | str | None = None
        self._pending_fetches: dict[str, Any] = {}
        self.last_rx_at: float | None = None       # monotonic — last clip.changed received
        self.last_tx_at: float | None = None       # monotonic — last clip.put sent upstream
        self.last_connect_at: float | None = None  # monotonic — last successful connect
        self.last_disconnect_at: float | None = None  # monotonic — last disconnect
        self.last_error: str | None = None         # last exception message
        self.connect_count: int = 0                # total successful connections
        self.rx_count: int = 0                     # total clip.changed received
        self.tx_count: int = 0                     # total clip.put sent upstream
        self.reconnect: bool = True                # whether to auto-reconnect on disconnect
        # Conflict-resolution clock sync: how far the upstream clock leads
        # ours (server_now - our_now), exchanged during the clip.watch
        # handshake. Stored in the inherited RPCHandler._peer_clock_offset
        # (None until the first successful watch on a connection), so the
        # inherited _normalize_peer_ts applies it.
        self._watch_sent_at_utc: datetime.datetime | None = None
        # Hostname stamped onto forwarded puts (constant for this process).
        self._hostname: str = socket.gethostname()
        info(f"{self.__dict__}")

    # Marker read by InternalTopicData.is_remote — items ingested from this
    # client originate on a remote peer, so they lose ties against local writes.
    is_remote_source: bool = True

    # Monitoring classification (see Interface.monitor_kind).
    monitor_kind: str = "proxy"

    @override
    def monitor_addr(self) -> str | None:
        return self.url

    @property
    @override
    def name(self) -> str:
        return "ProxyClient"

    def _update_clock_offset(self, raw_result: object) -> None:
        """Estimate the upstream clock offset from the watch reply.

        The reply carries ``server_now_utc`` — the upstream's wall clock at the
        moment it processed our watch. We approximate the matching point on our
        own clock as the midpoint between sending the watch and receiving the
        reply (Cristian's algorithm), so ``offset = server_now - our_mid``
        cancels roughly half the round-trip delay. The offset is then used to
        translate upstream timestamps into our clock for "newer wins".
        """
        server_now: datetime.datetime | None = None
        if isinstance(raw_result, dict):
            server_now = parse_utc_timestamp(raw_result.get("server_now_utc"))
        if server_now is None:
            # Upstream did not report its clock (older peer) — leave offset at
            # its previous value (or None), so timestamps are compared as-is.
            debug("proxy: upstream did not report server_now_utc; "
                  "clock offset unchanged")
            return
        recv = parse_utc_timestamp(utc_timestamp())
        sent = self._watch_sent_at_utc
        if recv is not None and sent is not None:
            our_mid = sent + (recv - sent) / 2
        else:
            our_mid = recv or sent
        if our_mid is None:
            return
        self._peer_clock_offset = server_now - our_mid
        info("proxy: upstream clock offset = %.3fs (server leads)",
             self._peer_clock_offset.total_seconds())

    @property
    def _lazy_upstream_threshold(self) -> int:
        return int(os.environ.get("RCLIPBOARD_LAZY_UPSTREAM_KB", "0")) * 1024

    @property
    def _upstream_put_mode(self) -> str:
        return os.environ.get("RCLIPBOARD_UPSTREAM_PUT_MODE", "immediate")

    @property
    def _upstream_sync_delay_ms(self) -> int:
        return int(os.environ.get("RCLIPBOARD_UPSTREAM_SYNC_DELAY_MS", "5000"))

    @override
    async def send(self, data: TopicData):
        threshold = self._lazy_upstream_threshold
        val_len = len(data.value.value.encode())
        mode = self._upstream_put_mode if (threshold == 0 or val_len >= threshold) else "immediate"

        if mode == "immediate":
            item = topic_data_to_clipboard_item(data)
            await self.send_clip(item, meta=data.meta)
        elif mode == "debounced":
            await self._schedule_debounced_put(data)
        elif mode == "on_demand":
            # notify upstream that new data exists (stub clip.changed via clip.put with stub flag)
            # upstream will clip.get when it wants the value
            stub_item = topic_data_to_clipboard_item(data)
            stub_meta = dict(data.meta)
            stub_meta["stub"] = True
            await self.send_clip(stub_item, meta=stub_meta)

    async def _schedule_debounced_put(self, data: TopicData) -> None:
        topic = data.topic
        existing = self._pending_fetches.get(f"_dput_{topic}")
        if existing is not None and not existing.done():
            existing.cancel()  # type: ignore[union-attr]
        task = a.create_task(self._debounced_put_task(data), name=f"dput_{topic}")
        self._pending_fetches[f"_dput_{topic}"] = task  # type: ignore[assignment]

    async def _debounced_put_task(self, data: TopicData) -> None:
        await a.sleep(self._upstream_sync_delay_ms / 1000.0)
        item = topic_data_to_clipboard_item(data)
        await self.send_clip(item, meta=data.meta)

    @override
    async def _send_result(self, request_id: RPCId, result: JsonValue) -> None:
        await self._send_json({"jsonrpc": "2.0", "id": request_id, "result": result})

    @override
    async def _send_error(self, request_id: RPCId, rpc_error: RPCError) -> None:
        await self._send_json({
            "jsonrpc": "2.0",
            "id": request_id,
            "error": rpc_error.model_dump(mode="json"),
        })

    @override
    async def _send_event(self, method: str, params: object) -> None:
        await self._send_json({"jsonrpc": "2.0", "method": method, "params": params})

    async def _send_json(self, payload: dict[str, JsonValue]) -> None:
        if self.ws is None:
            raise RuntimeError("proxy websocket is not connected")
        await self.ws.send(json.dumps(payload))

    async def _watch(self) -> None:
        self._watch_id = next_id()
        # Carry our current UTC time so the upstream can compute the clock
        # offset symmetrically; record it to estimate the offset on the reply.
        now = utc_timestamp()
        self._watch_sent_at_utc = parse_utc_timestamp(now)
        await self._send_json({
            "jsonrpc": "2.0",
            "id": self._watch_id,
            "method": "clip.watch",
            "params": {
                "topics": list(self.topics),
                "peer_now_utc": now,
            },
        })

    async def send_clip(self,
                        item: ClipboardItem,
                        meta: dict[str, JsonValue] | None = None):
        if not self.connected:
            return
        req_id = next_id()
        # Tag every forwarded put so the upstream can tell it arrived via a proxy
        # and from which host (e.g. for audit / multi-peer echo handling). Copy
        # first — `meta` may be the caller's `data.meta`, shared with local state.
        meta = dict(meta or {})
        meta["via"] = "proxy"
        meta["host"] = self._hostname
        await self._send_json({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "clip.put",
            "params": {
                "items": [item.model_dump(mode="json")],
                "meta": meta,
            },
        })
        self.last_tx_at = a.get_event_loop().time()
        self.tx_count += 1
        debug(f"proxy→upstream clip.put topic={item.topic} size={len(item.value.encode())} tx_at={self.last_tx_at:.3f}")

    async def _upstream_clip_get(self, topic: str) -> TopicData | None:
        """Fetch full value from upstream via WS clip.get. Returns None if unavailable."""
        if not self.connected or self.ws is None:
            return None
        req_id = next_id()
        future: a.Future[TopicData | None] = a.get_event_loop().create_future()
        self._pending_fetches[str(req_id)] = future  # type: ignore[assignment]
        try:
            await self._send_json({
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "clip.get",
                "params": {"topic": topic},
            })
            return await a.wait_for(future, timeout=10.0)
        except a.TimeoutError:
            return None
        finally:
            self._pending_fetches.pop(str(req_id), None)

    @override
    async def _handle_clip_get(self, request: JSONRPCRequestMessage) -> ClipGetResult:
        params_raw = request.params or {}
        topic = str(params_raw.get("topic", "")) if isinstance(params_raw, dict) else ""
        content = await enqueue_request_topic(self.app, topic)
        if content is not None and content.stub:
            # local store has only a stub — fetch from upstream on demand
            full = await self._upstream_clip_get(topic)
            if full is None:
                raise RPCMethodError(5031, "upstream unavailable")
            await enqueue_topic_data(self.app, data=full, source=self)
            content = full
        if content is None:
            raise RPCMethodError(1001, "Topic not found", {"topic": topic})
        return ClipGetResult(item=_topic_data_to_clipboard_item(content))

    async def _handle_response(self, message: dict[str, JsonValue]) -> None:
        message_id = message.get("id")
        pending_future = self._pending_fetches.pop(str(message_id), None)
        if pending_future is not None and not pending_future.done():
            if "error" in message:
                pending_future.set_result(None)
            else:
                try:
                    result = ClipGetResult.model_validate(message.get("result"))
                    topic_data = _clipboard_item_to_topic_data(result.item, meta={})
                    pending_future.set_result(topic_data)
                except Exception:
                    pending_future.set_result(None)
            return
        if message_id == self._watch_id:
            if "error" in message:
                warning(
                    f"proxy watch rejected by upstream: {message['error']}")
            else:
                info(f"proxy subscribed upstream topics: {self.topics}")
                result = ClipWatchResult.model_validate(message.get("result"))
                self._update_clock_offset(message.get("result"))
                threshold = self._lazy_upstream_threshold
                for _, topic_data in result.contents.items():
                    compare_ts = self._normalize_peer_ts(topic_data)
                    val_len = len(topic_data.value.value.encode())
                    if threshold > 0 and val_len >= threshold:
                        topic_data = stub_topic_data(topic_data)
                    await enqueue_topic_data_nowait(
                        self.app,
                        data=topic_data,
                        source=self,
                        compare_ts=compare_ts,
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
        threshold = self._lazy_upstream_threshold
        for raw_item in raw_items:
            item = ClipboardItem.model_validate(raw_item)
            topic_data = _clipboard_item_to_topic_data(item, meta=meta)
            compare_ts = self._normalize_peer_ts(topic_data)
            val_len = len(topic_data.value.value.encode())
            if threshold > 0 and val_len >= threshold:
                # store stub locally — full value fetched on clip.get
                topic_data = stub_topic_data(topic_data)
            await enqueue_topic_data_nowait(
                self.app,
                data=topic_data,
                source=self,
                compare_ts=compare_ts,
            )
        self.last_rx_at = a.get_event_loop().time()
        self.rx_count += 1
        topics_received = [ClipboardItem.model_validate(r).topic for r in raw_items if isinstance(r, dict)]
        tx_lag = (self.last_rx_at - self.last_tx_at) if self.last_tx_at else None
        debug(f"proxy←upstream clip.changed topics={topics_received} rx_at={self.last_rx_at:.3f} lag={tx_lag:.3f}s" if tx_lag is not None else f"proxy←upstream clip.changed topics={topics_received} rx_at={self.last_rx_at:.3f}")

    async def run_loop(self):
        ws_cm: Any
        _max_size = 64 * 1024 * 1024  # 64 MiB — websockets default (1 MiB) drops large payloads
        if self.unix:
            debug(f"unix_connect: path: {self.path} uri={self.url}")
            ws_cm = unix_connect(path=str(self.path), uri=self.url, max_size=_max_size)
        else:
            ssl_ctx = _make_upstream_ssl_ctx()
            ws_cm = (connect(self.url, ssl=ssl_ctx, max_size=_max_size)
                     if ssl_ctx is not None else connect(self.url, max_size=_max_size))

        async with ws_cm as ws:
            self.ws = ws
            self.connected = True
            self.app.state.proxy_connected = True
            self.last_connect_at = a.get_event_loop().time()
            self.connect_count += 1
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
                    if decoded.get("id") is not None:
                        # request from upstream — handle symmetrically
                        try:
                            request = JSONRPCRequestMessage.model_validate(decoded)
                            await self.handle_request(request)
                        except Exception as exc:
                            await self._send_error(
                                decoded.get("id"),
                                RPCError(code=1000, message="Invalid Request",
                                         data=str(exc)),
                            )
                    else:
                        # notification (clip.changed) — no id, no response needed
                        await self._handle_event(decoded)
                elif "result" in decoded or "error" in decoded:
                    await self._handle_response(decoded)

    async def run(self):
        backoff = 1.0
        error_state = False
        while True:
            try:
                await self.run_loop()
                error_state = False
                backoff = 1.0
            except a.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                if not error_state:
                    warning(f"proxy upstream error: {exc}", exc_info=True)
                    error_state = True
            finally:
                self.connected = False
                self.ws = None
                self.app.state.proxy_connected = False
                self.last_disconnect_at = a.get_event_loop().time()
            if not self.reconnect:
                info("proxy auto-reconnect disabled — stopping")
                return
            await a.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


def _make_upstream_ssl_ctx() -> ssl.SSLContext | None:
    """Return an SSL context for the upstream WSS connection, or None for default behaviour.

    Set ``RCLIPBOARD_SSL_CA_BUNDLE`` to the path of a CA certificate file
    (PEM) to trust a specific CA — useful for self-signed upstream certs in
    dev/test environments.
    """
    ca_bundle = os.environ.get("RCLIPBOARD_SSL_CA_BUNDLE")
    if ca_bundle:
        return ssl.create_default_context(cafile=ca_bundle)
    return None


def _make_ws_url() -> dict[str, str | bool | Path]:
    endpoint = upstream_endpoint_from_env()
    if endpoint.scheme == "uds":
        return {
            "url": "ws://localhost/ws",
            "path": endpoint.path,
            "unix": True,
        }
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
    register_client(app, client)
    subscribe_client(app, client, list(client.topics))
    app.state.proxy_task = a.create_task(client.run(), name="proxy_upstream")


async def shutdown_proxy(app: FastAPI) -> None:
    task = getattr(app.state, "proxy_task", None)
    if not task:
        return
    # Disable auto-reconnect first so loop termination doesn't rely on
    # cancellation timing relative to run()'s `if not self.reconnect` check.
    client: ProxyClient | None = getattr(app.state, "proxy_client", None)
    if client:
        client.reconnect = False
    task.cancel()
    with contextlib.suppress(a.CancelledError):
        await task
    if client:
        unsubscribe_client(app, client, list(client.topics))
        await client.stop_drainer()
        unregister_client(app, client)
    app.state.proxy_task = None
    app.state.proxy_connected = False
    app.state.proxy_enabled = False


async def connect_proxy(app: FastAPI, endpoint: str, reconnect: bool = True) -> None:
    """Disconnect current upstream (if any) and connect to a new one."""
    await shutdown_proxy(app)

    ws_kwargs = _make_ws_url_from_endpoint(endpoint)
    client = ProxyClient(app, **ws_kwargs)
    client.reconnect = reconnect
    app.state.proxy_client = client
    app.state.proxy_enabled = True
    register_client(app, client)
    subscribe_client(app, client, list(client.topics))
    app.state.proxy_task = a.create_task(client.run(), name="proxy_upstream")


async def disconnect_proxy(app: FastAPI) -> None:
    """Disconnect upstream and disable auto-reconnect."""
    client: ProxyClient | None = getattr(app.state, "proxy_client", None)
    if client:
        client.reconnect = False
    await shutdown_proxy(app)


def _make_ws_url_from_endpoint(endpoint: str) -> dict[str, str | bool | Path]:
    from rclipboard.endpoints import parse_endpoint
    ep = parse_endpoint(endpoint)
    if ep.scheme == "uds":
        return {"url": "ws://localhost/ws", "path": ep.path or "", "unix": True}
    if ep.scheme in {"https", "wss"}:
        return {"url": f"wss://{ep.host}:{ep.port}/ws"}
    return {"url": f"ws://{ep.host}:{ep.port}/ws"}


def get_proxy_status(app: FastAPI) -> dict[str, JsonValue]:
    import asyncio as _a
    conn: ProxyClient | None = getattr(app.state, "proxy_client", None)
    if conn is None:
        return {"enabled": False, "good": False}

    now = _a.get_event_loop().time()

    def _age(ts: float | None) -> float | None:
        return round(now - ts, 3) if ts is not None else None

    pending_fetches = sum(
        1 for k, v in conn._pending_fetches.items()
        if not k.startswith("_dput_") and hasattr(v, "done") and not v.done()
    )
    pending_puts = sum(
        1 for k, v in conn._pending_fetches.items()
        if k.startswith("_dput_") and hasattr(v, "done") and not v.done()
    )

    sync_direction: str | None = None
    if conn.last_rx_at is not None and conn.last_tx_at is not None:
        sync_direction = "rx" if conn.last_rx_at > conn.last_tx_at else "tx"
    elif conn.last_rx_at is not None:
        sync_direction = "rx"
    elif conn.last_tx_at is not None:
        sync_direction = "tx"

    return {
        "enabled": True,
        "good": conn.connected,
        "url": conn.url,
        "path": conn.path,
        "unix": conn.unix,
        "topics": list(conn.topics),
        "connect_count": conn.connect_count,
        "last_connect_ago": _age(conn.last_connect_at),
        "last_disconnect_ago": _age(conn.last_disconnect_at),
        "last_rx_ago": _age(conn.last_rx_at),
        "last_tx_ago": _age(conn.last_tx_at),
        "rx_count": conn.rx_count,
        "tx_count": conn.tx_count,
        "sync_direction": sync_direction,
        "pending_fetches": pending_fetches,
        "pending_puts": pending_puts,
        "last_error": conn.last_error,
    }
