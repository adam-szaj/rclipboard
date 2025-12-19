from __future__ import annotations
import copy
from pydantic import BaseModel

from app.types import TopicData, InternalTopicData, RequestMessage, ResponseMessage
from .log import get_logger
from .app_state import (
    enqueue_topic_data,
    enqueue_request_topic,
    enqueue_request_topics,
)

from typing import Any
import json as j

from fastapi import FastAPI, Query
from fastapi import HTTPException
from messages import (
    next_id,
)

from logging import Logger
from .log import get_logger

import sys

current_module = sys.modules[__name__]

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
    #    "enabled": getattr(app.state, "xsel_enabled", False),
    #    "path": getattr(app.state, "xsel_config", {}).get("path"),
    #    "interval_ms": getattr(app.state, "xsel_config",
    #                     {}).get("interval_ms"),
    #    "last_poll_ts": getattr(app.state, "xsel_last_poll_ts", None),
    #    "last_seen_ts": getattr(app.state, "xsel_last_seen_ts", {}),
    #    "last_applied_ts": getattr(app.state, "xsel_last_applied_ts", {}),
    #    "health": getattr(app.state, "xsel_health", None),
    # }
    return {"ok": True, "topics": [], "clients": [], "xsel": {}}


async def topics(app: FastAPI) -> dict[str, Any]:
    return {"topics": sorted(await enqueue_request_topics(app))}


async def get_clip(
    app: FastAPI, topic: str
) -> TopicData:
    info("before enqueue_request_topic")
    content: TopicData | None = await enqueue_request_topic(app, topic)
    info(f"after enqueue_request_topic: {content}")
    if content is None:
        raise HTTPException(status_code=404)
    return content


async def clip(
    app: FastAPI,
    data: TopicData,
) -> TopicData:
    await enqueue_topic_data(app, data=data, source=None)
    return data


def install_http_handlers(app: FastAPI) -> None:
    @app.get("/health")
    async def _health():
        xh = getattr(app.state, "xsel_health", None) or {}
        xsel_ok = bool(xh.get("ok", False)) if isinstance(xh, dict) else False
        proxy_client = getattr(app.state, "proxy_client", None)
        proxy_connected = bool(getattr(proxy_client, "connected", False))
        return {"ok": True, "xsel_ok": xsel_ok, "proxy_connected": proxy_connected}

    @app.get("/status")
    async def _status():
        return await status(app)

    @app.get("/topics")
    async def _topics():
        return await topics(app)

    @app.get("/clip/{topic}", response_model=TopicData)
    async def a_get_clip(
        topic: str,
    ) -> TopicData:
        debug(f"calling get_clip topic: {topic}")
        return await get_clip(app, topic)

    @app.post("/clip", response_model=TopicData)
    async def _clip(body: TopicData):
        info(f"body: {body}")
        return await clip(app, body)
