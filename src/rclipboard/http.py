from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
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
from .types import Interface

error = (get_logger(__name__)).error
warning = (get_logger(__name__)).warning
info = (get_logger(__name__)).info
debug = (get_logger(__name__)).debug
trace = (get_logger(__name__)).debug
if hasattr(get_logger(__name__), "trace"):
    trace = getattr(get_logger(__name__), "trace")

_topic_data_to_clipboard_item = topic_data_to_clipboard_item
_clipboard_item_to_topic_data = clipboard_item_to_topic_data


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
        info(f"request from: {request.client} headers: {request.headers}")

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
        return ClipGetResult(item=_topic_data_to_clipboard_item(content))

    @app.post("/v1/clip.put", response_model=ClipPutResult)
    async def _post_clip_put(body: ClipPutParams,
                             request: Request) -> ClipPutResult:
        info(f"request from: {request.client} headers: {request.headers}")
        items: list[ClipboardItem] = []
        for item in body.items:
            topic_data = _clipboard_item_to_topic_data(item, meta=body.meta)
            await enqueue_topic_data(app, data=topic_data, source=None)
            items.append(_topic_data_to_clipboard_item(topic_data))
        return ClipPutResult(items=items)
