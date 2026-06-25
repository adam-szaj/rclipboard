from __future__ import annotations

import contextlib
import datetime
import hashlib
import os
import time

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from rclipboard.helpers import (
    clipboard_item_to_topic_data,
    topic_data_to_clipboard_item,
)
from rclipboard.types import (
    ClipboardItem,
    ClipGetParams,
    ClipGetResult,
    ClipPutParams,
    ClipPutResult,
    HealthResult,
    KeyEntry,
    KeyPublishParams,
    KeyPublishResult,
    KeysListResult,
    MonitorEventKind,
    ProxyConnectParams,
    ProxyConnectResult,
    ProxyDisconnectResult,
    StatusResult,
    TopicData,
    TopicsListParams,
    TopicsListResult,
)

from .app_state import (
    enqueue_request_topic,
    enqueue_request_topics,
    enqueue_topic_data,
)
from .log import get_logger

error = (get_logger(__name__)).error
warning = (get_logger(__name__)).warning
info = (get_logger(__name__)).info
debug = (get_logger(__name__)).debug
trace = (get_logger(__name__)).debug
if hasattr(get_logger(__name__), "trace"):
    trace = getattr(get_logger(__name__), "trace")

_topic_data_to_clipboard_item = topic_data_to_clipboard_item
_clipboard_item_to_topic_data = clipboard_item_to_topic_data


def _build_monitor_snapshot(app: FastAPI) -> dict:
    from rclipboard.proxy import get_proxy_status
    main = app.state.main
    now = time.monotonic()
    ts_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()

    clients = []
    for iface, ci in main.client_info.items():
        connected_ago = round(now - ci.connected_at, 3)
        clients.append({
            "conn_id": ci.conn_id,
            "kind": ci.kind,
            "addr": ci.addr,
            "app": ci.app,
            "connected_ago": connected_ago,
            "topics": sorted(ci.topics),
            "put_count": ci.put_count,
            "get_count": ci.get_count,
            "notify_count": ci.notify_count,
            "last_put_ago": round(now - ci.last_put_at, 3) if ci.last_put_at else None,
            "last_get_ago": round(now - ci.last_get_at, 3) if ci.last_get_at else None,
            "last_notify_ago": round(now - ci.last_notify_at, 3) if ci.last_notify_at else None,
        })

    topics = []
    for tm in main.topic_meta.values():
        stored_ago = round(now - tm.stored_at, 3)
        notified = {cid: round(now - t, 3) for cid, t in tm.notified_clients.items()}
        topics.append({
            "topic": tm.topic,
            "size": tm.size,
            "stored_ago": stored_ago,
            "stored_at_utc": tm.stored_at_utc,
            "source_id": tm.source_id,
            "source_app": tm.source_app,
            "source_addr": tm.source_addr,
            "get_count": tm.get_count,
            "notify_count": tm.notify_count,
            "notified_clients_ago": notified,
            "last_get_ago": round(now - tm.last_get_at, 3) if tm.last_get_at else None,
            "last_get_by": tm.last_get_by,
        })

    return {
        "ts_utc": ts_utc,
        "clients": clients,
        "topics": topics,
        "proxy": get_proxy_status(app),
    }


async def _http_exception_handler(_request: Request,
                                  exc: StarletteHTTPException):
    if isinstance(exc.detail, dict) and {"code", "message"} <= set(exc.detail):
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.status_code,
            "message": str(exc.detail)
        },
    )


def install_module(app: FastAPI):

    @app.get("/health")
    @app.get("/v1/health.get", response_model=HealthResult)
    async def _health(request: Request):
        info(f"request from: {request.client}")
        return app.state.main.get_health()

    @app.get("/status")
    @app.get("/v1/status.get", response_model=StatusResult)
    async def _get_status(request: Request):
        info(f"request from: {request.client}")
        topics = list(sorted(await enqueue_request_topics(app) or []))
        return app.state.main.get_status(topics)

    @app.get("/topics")
    @app.post("/v1/topics.list", response_model=TopicsListResult)
    async def _get_topics(
            request: Request,
            body: TopicsListParams | None = None) -> TopicsListResult:
        info(f"request from: {request.client}")
        _ = body
        return TopicsListResult(
            topics=list(sorted(await enqueue_request_topics(app) or [])))

    @app.get("/v1/clip/{topic}", response_model=TopicData)
    async def _get_clip(topic: str, request: Request) -> TopicData:
        info(f"request from: {request.client}")
        content: TopicData | None = await enqueue_request_topic(app, topic)
        if content is None:
            raise HTTPException(status_code=404)
        return content

    @app.post("/v1/clip.get", response_model=ClipGetResult)
    async def _post_get_clip(body: ClipGetParams,
                             request: Request) -> ClipGetResult:
        debug(f"clip.get from: {request.client}")

        content: TopicData | None = await enqueue_request_topic(
            app, body.topic)
        if content is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": 1001,
                    "message": "Topic not found",
                    "data": {
                        "topic": body.topic
                    },
                },
            )
        item = _topic_data_to_clipboard_item(content)
        if item.encrypted:
            pub_key = request.headers.get("x-age-public-key", "")
            if not pub_key or pub_key not in app.state.main.public_keys:
                raise HTTPException(
                    status_code=403,
                    detail={"code": 4032, "message": "Not registered"},
                )
        return ClipGetResult(item=item)

    @app.post("/v1/keys.publish", response_model=KeyPublishResult)
    async def _keys_publish(body: KeyPublishParams,
                            request: Request) -> KeyPublishResult:
        info(f"request from: {request.client}")
        token = os.environ.get("RCLIPBOARD_ADMIN_TOKEN", "")
        if not token:
            raise HTTPException(
                status_code=503,
                detail={"code": 5031, "message": "Key registry not enabled"},
            )
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {token}":
            raise HTTPException(
                status_code=403,
                detail={"code": 4031, "message": "Forbidden"},
            )
        key_id = hashlib.sha256(body.public_key.encode()).hexdigest()[:16]
        app.state.main.public_keys[body.public_key] = {
            "public_key": body.public_key,
            "label": body.label,
            "key_id": key_id,
        }
        return KeyPublishResult(ok=True, key_id=key_id)

    @app.get("/v1/keys.list", response_model=KeysListResult)
    async def _keys_list(request: Request) -> KeysListResult:
        info(f"request from: {request.client}")
        entries = [KeyEntry(**v) for v in app.state.main.public_keys.values()]
        return KeysListResult(keys=entries)

    @app.post("/v1/proxy.connect", response_model=ProxyConnectResult)
    async def _proxy_connect(body: ProxyConnectParams,
                             request: Request) -> ProxyConnectResult:
        info(f"request from: {request.client}")
        token = os.environ.get("RCLIPBOARD_ADMIN_TOKEN", "")
        if not token:
            raise HTTPException(
                status_code=503,
                detail={"code": 5031, "message": "Admin token not configured"},
            )
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {token}":
            raise HTTPException(
                status_code=403,
                detail={"code": 4031, "message": "Forbidden"},
            )
        from rclipboard.proxy import connect_proxy
        await connect_proxy(app, body.endpoint, reconnect=body.reconnect)
        return ProxyConnectResult(ok=True, endpoint=body.endpoint)

    @app.post("/v1/proxy.disconnect", response_model=ProxyDisconnectResult)
    async def _proxy_disconnect(request: Request) -> ProxyDisconnectResult:
        info(f"request from: {request.client}")
        token = os.environ.get("RCLIPBOARD_ADMIN_TOKEN", "")
        if not token:
            raise HTTPException(
                status_code=503,
                detail={"code": 5031, "message": "Admin token not configured"},
            )
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {token}":
            raise HTTPException(
                status_code=403,
                detail={"code": 4031, "message": "Forbidden"},
            )
        from rclipboard.proxy import disconnect_proxy
        await disconnect_proxy(app)
        return ProxyDisconnectResult(ok=True)

    @app.post("/v1/clip.put", response_model=ClipPutResult)
    async def _post_clip_put(body: ClipPutParams,
                             request: Request) -> ClipPutResult:
        debug(f"clip.put from: {request.client}")
        client = request.client
        meta_app = str(body.meta["app"]) if body.meta and body.meta.get("app") else None
        if client and client.host:
            conn_id = f"http:{client.host}:{client.port}"
        else:
            conn_id = f"http:{meta_app}" if meta_app else "http:local"
        items: list[ClipboardItem] = []
        for item in body.items:
            topic_data = _clipboard_item_to_topic_data(item, meta=body.meta)
            await enqueue_topic_data(app, data=topic_data, source=None,
                                     monitor_conn_id=conn_id,
                                     monitor_app=meta_app)
            items.append(_topic_data_to_clipboard_item(topic_data))
        return ClipPutResult(items=items)

    @app.get("/v1/monitor.snapshot")
    async def _monitor_snapshot(request: Request):
        info(f"request from: {request.client}")
        return _build_monitor_snapshot(app)

    @app.websocket("/v1/monitor.stream")
    async def _monitor_stream(ws: WebSocket):
        await ws.accept()
        main = app.state.main
        q = main.subscribe_monitor()
        try:
            await ws.send_json({"kind": "snapshot", "data": _build_monitor_snapshot(app)})
            while True:
                event = await q.get()
                await ws.send_json({
                    "kind": event.kind.value,
                    "ts_utc": event.ts_utc,
                    "conn_id": event.conn_id,
                    "topic": event.topic,
                    "data": event.data,
                })
                if event.kind == MonitorEventKind.SERVICE_STOP:
                    # Server is shutting down — deliver the notice, then close so
                    # this handler returns and its task leaves server_state.tasks
                    # (otherwise it stays parked in `await q.get()` forever).
                    with contextlib.suppress(Exception):
                        await ws.close(code=1001)  # going away
                    break
        except WebSocketDisconnect:
            pass
        finally:
            main.unsubscribe_monitor(q)
