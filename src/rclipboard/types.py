import asyncio as a
import contextlib
import datetime
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Annotated, Literal, override

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

_TOPIC_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class Interface(ABC):

    def __init__(self):
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @override
    def __repr__(self) -> str:
        return f"Interface[{self.name}]"


_logger = logging.getLogger(__name__)


class BidirectionalInterface(Interface):

    def __init__(self):
        super().__init__()
        self.topics: set[str] = set()
        self._pending: dict[str, "TopicData"] = {}
        self._pending_event: a.Event = a.Event()
        self._drainer_task: a.Task | None = None

    def deliver(self, data: "TopicData") -> None:
        """Enqueue latest value per topic — older pending values are replaced."""
        self._pending[data.topic] = data
        self._pending_event.set()

    async def _run_drainer(self) -> None:
        while True:
            await self._pending_event.wait()
            self._pending_event.clear()
            pending, self._pending = self._pending, {}
            for data in pending.values():
                try:
                    await self.send(data)
                except a.CancelledError:
                    raise
                except Exception as exc:
                    _logger.error("send to %s failed: %s",
                                  self.name,
                                  exc,
                                  exc_info=True)

    def start_drainer(self) -> None:
        if self._drainer_task is None or self._drainer_task.done():
            self._drainer_task = a.create_task(self._run_drainer(),
                                               name=f"drainer_{self.name}")

    async def stop_drainer(self) -> None:
        if self._drainer_task:
            self._drainer_task.cancel()
            with contextlib.suppress(a.CancelledError):
                await self._drainer_task
            self._drainer_task = None

    @abstractmethod
    async def send(self, data: "TopicData") -> None:
        pass


class ValueData(BaseModel):
    value: str
    value_type: Literal["text", "binary"] = Field(alias="type", default="text")
    value_encoding: Literal["plain", "hex", "base64"] = Field(alias="encoding",
                                                              default="plain")


class TopicData(BaseModel):
    topic: Annotated[str, Field(init=True)]
    value: Annotated[ValueData, Field(init=True)]
    meta: Annotated[dict[str, JsonValue], Field(init=True)]
    stub: bool = False          # value omitted — fetch via fetch_url
    fetch_url: str | None = None  # relative URL to retrieve full value

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @field_validator("topic")
    @classmethod
    def validate_topic(cls, v: str) -> str:
        if not _TOPIC_RE.match(v):
            raise ValueError(
                f"invalid topic name: {v!r} (must match {_TOPIC_RE.pattern})")
        return v


class DigestInfo(BaseModel):
    algo: str
    value: str


class ClipboardItem(BaseModel):
    topic: str
    value: JsonValue
    mime: str = "application/octet-stream"
    encoding: Literal["utf-8", "base64", "json"] = "base64"
    encrypted: bool = False
    size: int | None = None
    digest: DigestInfo | None = None

    @field_validator("topic")
    @classmethod
    def validate_topic(cls, v: str) -> str:
        if not _TOPIC_RE.match(v):
            raise ValueError(
                f"invalid topic name: {v!r} (must match {_TOPIC_RE.pattern})")
        return v


class KeyPublishParams(BaseModel):
    public_key: str
    label: str = ""


class KeyPublishResult(BaseModel):
    ok: bool
    key_id: str


class KeyEntry(BaseModel):
    key_id: str
    public_key: str
    label: str


class KeysListResult(BaseModel):
    keys: list[KeyEntry]


class RPCError(BaseModel):
    code: int
    message: str
    data: JsonValue | None = None


class ClipPutParams(BaseModel):
    items: list[ClipboardItem]
    meta: dict[str, str] = Field(default_factory=dict)


class ClipPutResult(BaseModel):
    items: list[ClipboardItem]


class ClipGetParams(BaseModel):
    topic: str


class ClipGetResult(BaseModel):
    item: ClipboardItem


class ClipWatchParams(BaseModel):
    topics: list[str]
    public_key: str | None = None
    # Watcher's current UTC time, used for clock-offset exchange (conflict
    # resolution). Optional — older peers omit it.
    peer_now_utc: str | None = None


class ClipWatchResult(BaseModel):
    topics: list[str] = Field(default_factory=list)
    contents: dict[str, TopicData] = Field(default_factory=dict)
    # Server's UTC time at the moment it handled the watch, so the watcher can
    # estimate the clock offset. Omitted (None) keeps the field out of the wire.
    server_now_utc: str | None = None


class TopicsListParams(BaseModel):
    pass


class TopicsListResult(BaseModel):
    topics: list[str]


class HealthResult(BaseModel):
    ok: bool
    xsel_enabled: bool = False
    xsel_good: bool = False
    proxy_enabled: bool = False
    proxy_good: bool = False


class ProxyConnectParams(BaseModel):
    endpoint: str
    reconnect: bool = True


class ProxyConnectResult(BaseModel):
    ok: bool
    endpoint: str


class ProxyDisconnectResult(BaseModel):
    ok: bool


class TopicStatus(BaseModel):
    topic: str
    ts: str | None = None        # UTC ISO timestamp from meta["ts"]
    stub: bool = False           # True when only stub is stored locally
    size: int | None = None      # byte length of value (None when stub)
    stored_ago: float | None = None  # seconds since stored locally (monotonic)


class StatusResult(BaseModel):
    ok: bool
    topics: list[str] = Field(default_factory=list)
    topic_status: list[TopicStatus] = Field(default_factory=list)
    clients: list[str] = Field(default_factory=list)
    xsel: dict[str, JsonValue] = Field(default_factory=dict)
    proxy: dict[str, JsonValue] = Field(default_factory=dict)


class InternalTopicData(ABC):

    def __init__(self, data: TopicData, source: Interface | None,
                 monitor_conn_id: str | None = None,
                 monitor_app: str | None = None,
                 compare_ts: "datetime.datetime | None" = None):
        self.data: TopicData = data
        self.source: Interface | None = source
        self.stored_at: float = time.monotonic()
        self.stored_at_utc: str = data.meta.get("ts", "")  # type: ignore[assignment]
        self.monitor_conn_id: str | None = monitor_conn_id
        self.monitor_app: str | None = monitor_app
        # Timestamp used for conflict resolution ("newer wins"), already
        # normalised to *this* host's clock when the item arrived from a peer
        # with a known clock offset. ``None`` when no comparable ts exists.
        self.compare_ts: "datetime.datetime | None" = compare_ts
        # Set True by the dispatcher when conflict resolution drops this item
        # (older or losing a tie), so callers can tell it was not stored.
        self.rejected: bool = False

    @property
    def is_remote(self) -> bool:
        """True when this item originated from a remote peer (proxy upstream).

        Used for tie-breaking: on equal timestamps a local value supersedes a
        remote one. Detected via a duck-typed marker on the source interface to
        avoid importing the proxy module (circular import).
        """
        return bool(getattr(self.source, "is_remote_source", False))

    @property
    def topic(self) -> str:
        return self.data.topic


# ── Monitoring data structures ────────────────────────────────────────────────

@dataclass
class ClientInfo:
    conn_id: str
    kind: str                           # "ws" | "uds" | "proxy" | "xsel"
    addr: str | None
    app: str | None
    connected_at: float                 # monotonic
    topics: set[str] = field(default_factory=set)
    put_count: int = 0
    get_count: int = 0
    notify_count: int = 0
    last_put_at: float | None = None
    last_get_at: float | None = None
    last_notify_at: float | None = None


@dataclass
class TopicMeta:
    topic: str
    size: int | None
    stored_at: float                    # monotonic
    stored_at_utc: str
    source_id: str | None
    source_app: str | None
    source_addr: str | None
    get_count: int = 0
    notify_count: int = 0
    notified_clients: dict[str, float] = field(default_factory=dict)  # conn_id → last_notify_at
    last_get_at: float | None = None
    last_get_by: str | None = None


class MonitorEventKind(str, Enum):
    CLIENT_CONNECTED    = "client.connected"
    CLIENT_DISCONNECTED = "client.disconnected"
    CLIENT_SUBSCRIBED   = "client.subscribed"
    TOPIC_PUT           = "topic.put"
    TOPIC_GET           = "topic.get"
    TOPIC_NOTIFY        = "topic.notify"
    PROXY_CONNECTED     = "proxy.connected"
    PROXY_DISCONNECTED  = "proxy.disconnected"
    SERVICE_STOP        = "service.stop"


@dataclass
class MonitorEvent:
    kind: MonitorEventKind
    ts: float                           # monotonic
    ts_utc: str
    conn_id: str | None = None
    topic: str | None = None
    data: dict = field(default_factory=dict)


RPCId = int | str | None


class Message(BaseModel):
    pass


class JSONRPCRequestMessage(Message):
    jsonrpc: Literal["2.0"]
    mid: Annotated[RPCId, Field(default=None, alias="id")] = None
    method: Literal[
        "clip.put",
        "clip.get",
        "clip.watch",
        "clip.unwatch",
        "topics.list",
        "status.get",
        "health.get",
        "clip.changed",
    ]
    params: list[JsonValue] | dict[str, JsonValue] | None


class JSONRPCResponseMessage(Message):
    jsonrpc: Literal["2.0"]
    result: Annotated[JsonValue | None, Field(init=True, kw_only=True)] = None
    error: Annotated[RPCError | None, Field(init=True, kw_only=True)] = None
    mid: Annotated[RPCId, Field(default=None, alias="id", kw_only=True)]
