import asyncio as a
import contextlib
import logging
import re
import time
from abc import ABC, abstractmethod
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


class ClipWatchResult(BaseModel):
    topics: list[str] = Field(default_factory=list)
    contents: dict[str, TopicData] = Field(default_factory=dict)


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

    def __init__(self, data: TopicData, source: Interface | None):
        self.data: TopicData = data
        self.source: Interface | None = source
        self.stored_at: float = time.monotonic()
        self.stored_at_utc: str = data.meta.get("ts", "")  # type: ignore[assignment]

    @property
    def topic(self) -> str:
        return self.data.topic


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
