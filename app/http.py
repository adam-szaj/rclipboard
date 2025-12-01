from __future__ import annotations
import copy
from .log import get_logger
from .app_state import enqueue_topic_data, enqueue_request_topic, enqueue_request_topics, make_topic_data

from typing import Any
import json as j

from fastapi import Body, FastAPI, Query
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from messages import (
    utc_timestamp,
    makeResponse,
    makeResponseError,
    next_id,
    normalize_data_items,
)

from logging import Logger
from .log import get_logger

logger: Logger = get_logger(__name__)
error = logger.error
warning = logger.warning
info = logger.info
debug = logger.debug
trace = logger.debug


async def status(app: FastAPI) -> dict[str, Any]:
    topics = []
    # clients = sum(len(s) for s in app.state.subs.values())
    # xsel_info = {
    #     "enabled": getattr(app.state, "xsel_enabled", False),
    #     "path": getattr(app.state, "xsel_config", {}).get("path"),
    #     "interval_ms": getattr(app.state, "xsel_config",
    #                            {}).get("interval_ms"),
    #     "last_poll_ts": getattr(app.state, "xsel_last_poll_ts", None),
    #     "last_seen_ts": getattr(app.state, "xsel_last_seen_ts", {}),
    #     "last_applied_ts": getattr(app.state, "xsel_last_applied_ts", {}),
    #     "health": getattr(app.state, "xsel_health", None),
    # }
    return {"ok": True, "topics": [], "clients": [], "xsel": {}}


async def topics(app: FastAPI) -> dict[str, Any]:
    return {"topics": sorted(await enqueue_request_topics(app))}


async def get_clip(app: FastAPI, topic: str, json: bool, quiet: bool):
    info("before enqueue_request_topic")
    content = await enqueue_request_topic(app, topic)
    info(f"after enqueue_request_topic: {content}")

    if content is None:
        if json:
            info(f"JSONResponse content 1: '{content}'")
            req = {"id": next_id(), "method": "get"}
            if quiet:
                return JSONResponse({}, status_code=200)
            else:
                return JSONResponse({"message": f"clip '{topic}' not found"},
                                    status_code=404)

    else:
        if json:
            info(f"JSONResponse content: '{content}'")
            req = {"id": next_id(), "method": "get"}
            return JSONResponse(
                makeResponse(req,
                             value={
                                 "source": str(content.get("source")),
                                 "data": content.get("data"),
                                 "meta": content.get("meta")
                             }))

    info(f"PlainTextResponse content: '{content}'")
    return PlainTextResponse((content or {}).get("value", ""), status_code=200)


async def clip(
        app: FastAPI,
        topic: str,
        source: object,
        body: dict = Body(...),
        json: str | None = None,
):
    pid = body.get("id") or next_id()
    meta = body.get("meta", {})
    try:
        data_items = normalize_data_items(body.get("data"),
                                          topic_fallback=topic)
    except Exception as e:  # noqa: BLE001
        if json:
            req = {"id": pid, "method": "clip"}
            return JSONResponse(makeResponseError(req, {"message": str(e)}),
                                status_code=400)
        return PlainTextResponse(str(e), status_code=400)

    for data in data_items:
        topic_data = make_topic_data(source=str(source),
                                     topic=data.get("topic"),
                                     value=data.get("value", ""),
                                     type=data.get("type", ""),
                                     encoding=data.get("encoding", "base64"),
                                     app="http")

        await enqueue_topic_data(app, topic_data)

    if json:
        req = {"id": pid, "method": "clip"}
        return JSONResponse(
            makeResponse(req, value={"cliped": len(data_items)}))
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
        # return JSONResponse({"message": "Not Implemented"})

    @app.get("/topics")
    async def _topics():
        return await topics(app)

    @app.get("/clip/{topic}")
    async def _get_clip(topic: str,
                        json: bool = Query(default=False, alias="json"),
                        quiet: bool = Query(default=False, alias="quiet")):
        debug(f"calling get_clip json: {json} quiet: {quiet}")
        return await get_clip(app, topic, json, quiet)

    @app.post("/clip/{topic}")
    async def _clip(
            topic: str,
            body: dict = Body(...),
            json: bool = Query(default=True, alias="json"),
    ):
        info(f"body: {body}")
        body["ts"] = utc_timestamp()
        info(f"body: {body}")
        return await clip(app, topic, None, body, json)
