from __future__ import annotations

import json
from typing import override

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import JsonValue, ValidationError

from .app_state import (
    enqueue_request_topic,
    enqueue_request_topics,
    enqueue_topic_data,
    register_client,
    subscribe_client,
    unregister_client,
    unsubscribe_client,
)
from .helpers import clipboard_item_to_topic_data, topic_data_to_clipboard_item
from .log import get_logger as gl
from .types import (
    BidirectionalInterface,
    ClipboardItem,
    ClipGetParams,
    ClipGetResult,
    ClipPutParams,
    ClipPutResult,
    ClipWatchParams,
    ClipWatchResult,
    HealthResult,
    JSONRPCRequestMessage,
    JSONRPCResponseMessage,
    RPCError,
    StatusResult,
    TopicData,
    TopicsListResult,
)
from .xsel import get_xsel_status

logger = gl(__name__)
error = logger.error
warning = logger.warning
info = logger.info
debug = logger.debug
trace = logger.debug


class WSServerConnection(BidirectionalInterface):
    def __init__(self, app: FastAPI, ws: WebSocket):
        super().__init__()
        self.app: FastAPI = app
        self.ws: WebSocket = ws
        self.topics: set[str] = set()

    @property
    @override
    def name(self) -> str:
        return "wsconnection"

    async def send(self, data: TopicData):
        await self._send_event(
            "clip.changed",
            {
                "items": [
                    _topic_data_to_clipboard_item(data).model_dump(mode="json")
                ],
                "meta": data.meta,
            },
        )

    async def _send_result(
        self, request_id: int | str | None, result: JsonValue
    ) -> None:
        if request_id is None:
            return
        await self.ws.send_json(
            JSONRPCResponseMessage(
                mid=request_id,
                jsonrpc="2.0",
                result=result,
            ).model_dump(by_alias=True, mode="json", exclude_none=True)
        )

    async def _send_error(
        self, request_id: int | str | None, rpc_error: RPCError
    ) -> None:
        if request_id is None:
            return
        await self.ws.send_json(
            JSONRPCResponseMessage(
                mid=request_id,
                jsonrpc="2.0",
                error=rpc_error,
            ).model_dump(by_alias=True, mode="json", exclude_none=True)
        )

    async def _send_event(self, method: str, params: object) -> None:
        await self.ws.send_json(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
            }
        )

    async def _handle_clip_put(
        self, request: JSONRPCRequestMessage
    ) -> ClipPutResult:
        params = ClipPutParams.model_validate(request.params or {})
        items: list[ClipboardItem] = []
        for item in params.items:
            topic_data = _clipboard_item_to_topic_data(item, meta=params.meta)
            await enqueue_topic_data(self.app, data=topic_data, source=self)
            items.append(_topic_data_to_clipboard_item(topic_data))
        return ClipPutResult(items=items)

    async def _handle_clip_get(
        self, request: JSONRPCRequestMessage
    ) -> ClipGetResult:
        params = ClipGetParams.model_validate(request.params or {})
        content = await enqueue_request_topic(self.app, params.topic)
        if content is None:
            raise RPCMethodError(
                1001, "Topic not found", {"topic": params.topic}
            )
        return ClipGetResult(item=_topic_data_to_clipboard_item(content))

    async def _handle_clip_watch(
        self, request: JSONRPCRequestMessage
    ) -> ClipWatchResult:
        params = ClipWatchParams.model_validate(request.params or {})
        new_topics = [
            topic for topic in params.topics if topic not in self.topics
        ]
        contents = {}
        if new_topics:
            contents = subscribe_client(self.app, self, new_topics)
            self.topics.update(new_topics)
        return ClipWatchResult(topics=list(self.topics), contents=contents)

    async def _handle_clip_unwatch(
        self, request: JSONRPCRequestMessage
    ) -> ClipWatchResult:
        params = ClipWatchParams.model_validate(request.params or {})
        remove_topics = [
            topic for topic in params.topics if topic in self.topics
        ]
        if remove_topics:
            unsubscribe_client(self.app, self, remove_topics)
            self.topics.difference_update(remove_topics)
        return ClipWatchResult(topics=list(self.topics))

    async def _handle_topics_list(
        self, _request: JSONRPCRequestMessage
    ) -> TopicsListResult:
        topics = await enqueue_request_topics(self.app) or []
        return TopicsListResult(topics=list(sorted(topics)))

    async def _handle_status_get(
        self, _request: JSONRPCRequestMessage
    ) -> StatusResult:
        topics = await enqueue_request_topics(self.app) or []
        clients = [
            client.name
            for client in getattr(self.app.state.main, "clients", [])
            if hasattr(client, "name")
        ]
        return StatusResult(
            ok=True,
            topics=list(sorted(topics)),
            clients=clients,
            xsel=get_xsel_status(self.app),
        )

    async def _handle_health_get(
        self, _request: JSONRPCRequestMessage
    ) -> HealthResult:
        xsel = get_xsel_status(self.app)
        return HealthResult(
            ok=True,
            xsel_enabled=bool(xsel["enabled"]),
            xsel_good=bool(xsel["good"]),
        )

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
            debug(f"ws request error: {exc}", exc_info=True)
            await self._send_error(
                request.mid,
                RPCError(code=5000, message="Internal error"),
            )

    async def loop(self):
        await self.ws.accept()
        register_client(self.app, self)
        try:
            while True:
                json_message = await self.ws.receive_json()
                if not isinstance(json_message, dict):
                    continue
                if "method" not in json_message:
                    continue
                try:
                    request = JSONRPCRequestMessage.model_validate(
                        json_message
                    )
                except ValidationError as exc:
                    request_id = json_message.get("id")
                    await self._send_error(
                        request_id,
                        RPCError(
                            code=1000,
                            message="Invalid Request",
                            data=json.loads(exc.json()),
                        ),
                    )
                    continue
                await self.handle_request(request)
        except WebSocketDisconnect:
            pass
        finally:
            if self.topics:
                unsubscribe_client(self.app, self, list(self.topics))
                self.topics.clear()
            unregister_client(self.app, self)


class RPCMethodError(Exception):
    def __init__(self, code: int, message: str, data: object | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


_topic_data_to_clipboard_item = topic_data_to_clipboard_item
_clipboard_item_to_topic_data = clipboard_item_to_topic_data


async def install_module(app: FastAPI) -> None:
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        conn = WSServerConnection(app, ws)
        await conn.loop()
