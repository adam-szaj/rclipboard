from __future__ import annotations

from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from messages import (
    makeResponse,
    makeResponseError,
    next_id,
    normalize_data_items,
)
from . import xsel as xsel_mod


def _json_mode(response_type: str | None) -> bool:
    return response_type == "json"


async def status(app: FastAPI) -> dict[str, Any]:
    topics = sorted(app.state.subs.keys())
    clients = sum(len(s) for s in app.state.subs.values())
    xsel_info = {
        "enabled": getattr(app.state, "xsel_enabled", False),
        "path": getattr(app.state, "xsel_config", {}).get("path"),
        "interval_ms": getattr(app.state, "xsel_config",
                               {}).get("interval_ms"),
        "last_poll_ts": getattr(app.state, "xsel_last_poll_ts", None),
        "last_seen_ts": getattr(app.state, "xsel_last_seen_ts", {}),
        "last_applied_ts": getattr(app.state, "xsel_last_applied_ts", {}),
        "health": getattr(app.state, "xsel_health", None),
    }
    return {
        "ok": True,
        "topics": topics,
        "clients": clients,
        "xsel": xsel_info
    }


async def topics(app: FastAPI) -> dict[str, Any]:
    return {"topics": sorted(app.state.subs.keys())}


async def fetch(
    app: FastAPI,
    topic: str,
    response_type: str | None,
    id: int | None,
):
    json_mode = _json_mode(response_type)
    content = app.state.topic_content.get(topic)
    if content is None:
        if json_mode:
            req = {"id": id or next_id(), "method": "get"}
            return JSONResponse(makeResponseError(
                req, {"message": f"topic '{topic}' not found"}),
                                status_code=404)
        return PlainTextResponse(f"topic '{topic}' not found", status_code=404)

    if json_mode:
        req = {"id": id or next_id(), "method": "get"}
        return JSONResponse(makeResponse(req, value=content))
    return Response(status_code=200)


async def publish(
        app: FastAPI,
        topic: str,
        body: dict = Body(...),
        response_type: str | None = None,
):
    json_mode = _json_mode(response_type)
    pid = body.get("id") or next_id()
    meta = body.get("meta", {})
    try:
        data_items = normalize_data_items(body.get("data"),
                                          topic_fallback=topic)
    except Exception as e:  # noqa: BLE001
        if json_mode:
            req = {"id": pid, "method": "publish"}
            return JSONResponse(makeResponseError(req, {"message": str(e)}),
                                status_code=400)
        return PlainTextResponse(str(e), status_code=400)

    data = {"source": None, "meta": meta, "data_items": data_items}
    # print(f"got publish: {data}")
    await app.state.bus.put(data)
    if json_mode:
        req = {"id": pid, "method": "publish"}
        return JSONResponse(
            makeResponse(req, value={"published": len(data_items)}))
    return Response(status_code=202)


def install_http_handlers(app: FastAPI) -> None:

    @app.get("/health")
    async def _health():
        xh = getattr(app.state, "xsel_health", None) or {}
        xsel_ok = bool(xh.get("ok", False)) if isinstance(xh, dict) else False
        proxy_client = getattr(app.state, "proxy_client", None)
        proxy_connected = bool(getattr(proxy_client, "connected", False))
        return {
            "ok": True,
            "xsel_ok": xsel_ok,
            "proxy_connected": proxy_connected
        }

    @app.get("/status")
    async def _status():
        return await status(app)

    @app.get("/topics")
    async def _topics():
        return await topics(app)

    @app.get("/fetch/{topic}")
    async def _fetch(
        topic: str,
        response_type: str | None = Query(default=None, alias="response-type"),
        id: int | None = None,
    ):
        return await fetch(app, topic, response_type, id)

    @app.post("/publish/{topic}")
    async def _publish(
        topic: str,
        body: dict = Body(...),
        response_type: str | None = Query(default=None, alias="response-type"),
    ):
        return await publish(app, topic, body, response_type)
