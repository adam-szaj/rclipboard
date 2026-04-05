from __future__ import annotations

import json
from abc import abstractmethod
from typing import override

from fastapi import FastAPI
from pydantic import JsonValue, ValidationError

from rclipboard.app_state import (
    enqueue_request_topic,
    enqueue_request_topics,
    enqueue_topic_data,
    subscribe_client,
    unsubscribe_client,
)
from rclipboard.helpers import clipboard_item_to_topic_data, topic_data_to_clipboard_item
from rclipboard.log import get_logger as gl
from rclipboard.types import (
    BidirectionalInterface,
    ClipGetParams,
    ClipGetResult,
    ClipPutParams,
    ClipPutResult,
    ClipWatchParams,
    ClipWatchResult,
    HealthResult,
    JSONRPCRequestMessage,
    RPCError,
    RPCId,
    StatusResult,
    TopicData,
    TopicsListResult,
)

logger = gl(__name__)
error = logger.error
warning = logger.warning
info = logger.info
debug = logger.debug
trace = logger.debug

_topic_data_to_clipboard_item = topic_data_to_clipboard_item
_clipboard_item_to_topic_data = clipboard_item_to_topic_data


class RPCMethodError(Exception):

    def __init__(self, code: int, message: str, data: object | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class RPCHandler(BidirectionalInterface):

    def __init__(self, app: FastAPI):
        BidirectionalInterface.__init__(self)
        self.app = app
        self.topics: set[str] = set()
        self.public_key: str | None = None

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @abstractmethod
    async def _send_result(self, request_id: RPCId,
                           result: JsonValue) -> None:
        pass

    @abstractmethod
    async def _send_error(self, request_id: RPCId,
                          rpc_error: RPCError) -> None:
        pass

    @abstractmethod
    async def _send_event(self, method: str, params: object) -> None:
        pass

    @override
    async def send(self, data: TopicData) -> None:
        await self._send_event(
            "clip.changed",
            {
                "items":
                [_topic_data_to_clipboard_item(data).model_dump(mode="json")],
                "meta":
                data.meta,
            },
        )

    async def _handle_clip_put(
            self, request: JSONRPCRequestMessage) -> ClipPutResult:
        params = ClipPutParams.model_validate(request.params or {})
        items = []
        for item in params.items:
            topic_data = _clipboard_item_to_topic_data(item, meta=params.meta)
            await enqueue_topic_data(self.app, data=topic_data, source=self)
            items.append(_topic_data_to_clipboard_item(topic_data))
        return ClipPutResult(items=items)

    async def _handle_clip_get(
            self, request: JSONRPCRequestMessage) -> ClipGetResult:
        params = ClipGetParams.model_validate(request.params or {})
        content = await enqueue_request_topic(self.app, params.topic)
        if content is None:
            raise RPCMethodError(1001, "Topic not found",
                                 {"topic": params.topic})
        if content.meta.get("encrypted") is True:
            pub_key = self.public_key
            if not pub_key or pub_key not in self.app.state.main.public_keys:
                raise RPCMethodError(4032, "Not registered")
        return ClipGetResult(item=_topic_data_to_clipboard_item(content))

    async def _handle_clip_watch(
            self, request: JSONRPCRequestMessage) -> ClipWatchResult:
        params = ClipWatchParams.model_validate(request.params or {})
        if params.public_key:
            self.public_key = params.public_key
        new_topics = [
            topic for topic in params.topics if topic not in self.topics
        ]
        contents = {}
        if new_topics:
            contents = subscribe_client(self.app, self, new_topics)
            self.topics.update(new_topics)
        return ClipWatchResult(topics=list(self.topics), contents=contents)

    async def _handle_clip_unwatch(
            self, request: JSONRPCRequestMessage) -> ClipWatchResult:
        params = ClipWatchParams.model_validate(request.params or {})
        remove_topics = [
            topic for topic in params.topics if topic in self.topics
        ]
        if remove_topics:
            unsubscribe_client(self.app, self, remove_topics)
            self.topics.difference_update(remove_topics)
        return ClipWatchResult(topics=list(self.topics))

    async def _handle_topics_list(
            self, _request: JSONRPCRequestMessage) -> TopicsListResult:
        topics = await enqueue_request_topics(self.app) or []
        return TopicsListResult(topics=list(sorted(topics)))

    async def _handle_status_get(
            self, _request: JSONRPCRequestMessage) -> StatusResult:
        topics = await enqueue_request_topics(self.app) or []
        return self.app.state.main.get_status(topics)

    async def _handle_health_get(
            self, _request: JSONRPCRequestMessage) -> HealthResult:
        return self.app.state.main.get_health()

    async def handle_request(self, request: JSONRPCRequestMessage) -> None:
        try:
            match request.method:
                case "clip.put":
                    result = await self._handle_clip_put(request)
                case "clip.get":
                    result = await self._handle_clip_get(request)
                case "clip.watch":
                    result = await self._handle_clip_watch(request)
                case "clip.unwatch":
                    result = await self._handle_clip_unwatch(request)
                case "topics.list":
                    result = await self._handle_topics_list(request)
                case "status.get":
                    result = await self._handle_status_get(request)
                case "health.get":
                    result = await self._handle_health_get(request)
                case _:
                    raise RPCMethodError(
                        1003,
                        "Unsupported method",
                        {"method": request.method},
                    )
            await self._send_result(
                request.mid,
                result.model_dump(mode="json"),
            )
        except RPCMethodError as exc:
            exc_data = exc.data
            if not isinstance(exc_data, dict):
                exc_data = json.dumps(exc_data)

            await self._send_error(
                request.mid,
                RPCError(code=exc.code, message=exc.message, data=exc_data),
            )
        except ValidationError as exc:
            await self._send_error(
                request.mid,
                RPCError(
                    code=1000,
                    message="Invalid params",
                    data=json.loads(exc.json()),
                ),
            )
        except Exception as exc:
            debug(f"rpc request error: {exc}", exc_info=True)
            await self._send_error(
                request.mid,
                RPCError(code=5000, message="Internal error"),
            )
