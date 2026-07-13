from __future__ import annotations

import json
from abc import abstractmethod
from typing import override

from fastapi import FastAPI
from pydantic import JsonValue, ValidationError

from rclipboard.core.state import (
    enqueue_request_topic,
    enqueue_request_topics,
    enqueue_topic_data,
    subscribe_client,
    unregister_client,
    unsubscribe_client,
)
import datetime

from rclipboard.core.interfaces import BidirectionalInterface
from rclipboard.log import get_logger as gl
from rclipboard.models.convert import (
    clipboard_item_to_topic_data,
    stub_topic_data,
    topic_data_to_clipboard_item,
)
from rclipboard.models.rpc import JSONRPCRequestMessage, RPCId
from rclipboard.models.wire import (
    ClipGetParams,
    ClipGetResult,
    ClipPutParams,
    ClipPutResult,
    ClipWatchParams,
    ClipWatchResult,
    HealthResult,
    RPCError,
    StatusResult,
    TopicData,
    TopicsListResult,
)
from rclipboard.timeutil import parse_utc_timestamp, utc_timestamp

logger = gl(__name__)
error = logger.error
warning = logger.warning
info = logger.info
debug = logger.debug
trace = logger.debug

_topic_data_to_clipboard_item = topic_data_to_clipboard_item
_clipboard_item_to_topic_data = clipboard_item_to_topic_data


def _stub_if_large(td: TopicData, threshold: int) -> TopicData:
    if len(td.value.value.encode()) >= threshold:
        return stub_topic_data(td)
    return td


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
        self._is_rpc_transport: bool = True
        # How far this peer's clock leads ours (peer_now - our_now), learned
        # from the clip.watch handshake. Used to normalise the timestamps on
        # items this peer puts, so our "newer wins" resolution is skew-proof.
        self._peer_clock_offset: datetime.timedelta | None = None

    def _record_peer_clock(self, peer_now_utc: str, our_now_utc: str) -> None:
        """Record the connecting peer's clock offset from its watch.

        ``offset = peer_now - our_now`` (positive when the peer's clock leads
        ours). We then translate timestamps the peer stamps onto its items into
        our clock as ``ts - offset`` before comparing them locally.
        """
        peer_now = parse_utc_timestamp(peer_now_utc)
        our_now = parse_utc_timestamp(our_now_utc)
        if peer_now is None or our_now is None:
            return
        self._peer_clock_offset = peer_now - our_now
        debug("peer %s clock offset = %.3fs",
              self.name, self._peer_clock_offset.total_seconds())

    def _normalize_peer_ts(self, td: TopicData) -> datetime.datetime | None:
        """Translate a timestamp stamped by this peer into our local clock."""
        peer_ts = parse_utc_timestamp(td.meta.get("ts"))
        if peer_ts is None:
            return None
        if self._peer_clock_offset is None:
            return peer_ts
        return peer_ts - self._peer_clock_offset

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

    async def _dispatch_message(self, json_message: object) -> None:
        """Validate and dispatch one decoded JSON-RPC frame.

        Shared receive-path step for the ws/uds server connections: silently
        skips non-request frames, answers malformed requests with the JSON-RPC
        "Invalid Request" error, and routes valid ones to handle_request.
        """
        if not isinstance(json_message, dict) or "method" not in json_message:
            return
        try:
            request = JSONRPCRequestMessage.model_validate(json_message)
        except ValidationError as exc:
            await self._send_error(
                json_message.get("id"),
                RPCError(code=1000,
                         message="Invalid Request",
                         data=json.loads(exc.json())))
            return
        await self.handle_request(request)

    async def _teardown_connection(self) -> None:
        """Shared connection teardown: unsubscribe, drain, unregister."""
        if self.topics:
            unsubscribe_client(self.app, self, list(self.topics))
            self.topics.clear()
        await self.stop_drainer()
        unregister_client(self.app, self)

    async def _handle_clip_put(
            self, request: JSONRPCRequestMessage) -> ClipPutResult:
        params = ClipPutParams.model_validate(request.params or {})
        items = []
        for item in params.items:
            topic_data = _clipboard_item_to_topic_data(item, meta=params.meta)
            # Normalise the peer's own timestamp into our clock for conflict
            # resolution (skew-proof "newer wins"). Falls back to the raw ts
            # when no offset is known.
            compare_ts = self._normalize_peer_ts(topic_data)
            await enqueue_topic_data(self.app, data=topic_data, source=self,
                                     compare_ts=compare_ts)
            items.append(_topic_data_to_clipboard_item(topic_data))
        return ClipPutResult(items=items)

    async def _handle_clip_get(
            self, request: JSONRPCRequestMessage) -> ClipGetResult:
        params = ClipGetParams.model_validate(request.params or {})
        content = await enqueue_request_topic(self.app, params.topic,
                                              requester=self)
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
        # Capture our clock as early as possible so the offset estimate the
        # peer derives reflects when we actually handled the watch.
        server_now = utc_timestamp()
        params = ClipWatchParams.model_validate(request.params or {})
        if params.public_key:
            self.public_key = params.public_key
        if params.peer_now_utc:
            self._record_peer_clock(params.peer_now_utc, server_now)
        new_topics = [
            topic for topic in params.topics if topic not in self.topics
        ]
        contents: dict = {}
        if new_topics:
            contents = subscribe_client(self.app, self, new_topics)
            self.topics.update(new_topics)
        # Policy: encrypted values are never pushed to peers without a
        # registered public key. The clip.changed dispatch already filters
        # (see AppState._dispatch_data_item); the initial-sync contents must
        # not leak them either. The subscription itself stays — the peer will
        # start receiving the topic once its key is registered.
        key_registered = bool(
            self.public_key
            and self.public_key in self.app.state.main.public_keys)
        if not key_registered:
            contents = {
                topic: td for topic, td in contents.items()
                if td.meta.get("encrypted") is not True
            }
        threshold = self.app.state.main.lazy_local_threshold
        if threshold > 0:
            contents = {
                topic: _stub_if_large(td, threshold)
                for topic, td in contents.items()
            }
        return ClipWatchResult(topics=list(self.topics), contents=contents,
                               server_now_utc=server_now)

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
