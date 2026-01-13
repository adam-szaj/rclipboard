from __future__ import annotations

import sys
from typing import override

from fastapi import FastAPI, HTTPException, Request

from app.types import TopicData

from .app_state import enqueue_request_topic, enqueue_request_topics, enqueue_topic_data
from .log import get_logger
from .types import Connection

current_module = sys.modules[__name__]

error = (get_logger(__name__)).error
warning = (get_logger(__name__)).warning
info = (get_logger(__name__)).info
debug = (get_logger(__name__)).debug
trace = (get_logger(__name__)).debug
if hasattr(get_logger(__name__), "trace"):
    trace = getattr(get_logger(__name__), "trace")


class HTTPConnection(Connection):

    @property
    @override
    def name(self) -> str:
        return "http"

    @override
    async def send(self, data: object):
        pass


def install_http_handlers(app: FastAPI):

    @app.get("/health")
    async def _health(request: Request):
        info(f"request from: {request.client}")
        return "OK"

    @app.get("/status")
    async def _get_status(request: Request):
        info(f"request from: {request.client}")
        return {"ok": True, "topics": [], "clients": [], "xsel": {}}

    @app.get("/topics")
    async def _get_topics(request: Request):
        info(f"request from: {request.client}")
        jls_extract_var = {
            "topics": list(sorted(await enqueue_request_topics(app) or []))
        }
        return jls_extract_var

    @app.get("/clip/{topic}", response_model=TopicData)
    async def _get_clip(topic: str, request: Request) -> TopicData:
        info(f"request from: {request.client}")
        content: TopicData | None = await enqueue_request_topic(app, topic)
        if content is None:
            raise HTTPException(status_code=404)
        return content

    @app.post("/clip", response_model=TopicData)
    async def _post_clip(body: TopicData, request: Request):
        info(f"request from: {request.client}")
        _ = await enqueue_topic_data(app, data=body, source=None)
        return body
