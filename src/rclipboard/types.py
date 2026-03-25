import asyncio as a
import re
from abc import ABC, abstractmethod
from typing import Annotated, Literal, override

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

_TOPIC_RE = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')


class Interface(ABC):
    def __init__(self):
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    # @abstractmethod
    # async def send(self, data: object):
    #     pass

    @override
    def __repr__(self) -> str:
        return f"Interface[{self.name}]"


class BidirectionalInterface(Interface):
    def __init__(self):
        super().__init__()
        self.topics: set[str] = set()

    @abstractmethod
    async def send(self, data: object):
        pass

    pass


class ValueData(BaseModel):
    value: str
    value_type: Literal["text", "binary"] = Field(alias="type", default="text")
    value_encoding: Literal["plain", "hex", "base64"] = Field(
        alias="encoding", default="plain"
    )


class TopicData(BaseModel):
    topic: Annotated[str, Field(init=True)]
    value: Annotated[ValueData, Field(init=True)]
    meta: Annotated[dict[str, str], Field(init=True)]

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @field_validator('topic')
    @classmethod
    def validate_topic(cls, v: str) -> str:
        if not _TOPIC_RE.match(v):
            raise ValueError(f"invalid topic name: {v!r} (must match {_TOPIC_RE.pattern})")
        return v


class DigestInfo(BaseModel):
    algo: str
    value: str


class ClipboardItem(BaseModel):
    topic: str
    value: JsonValue
    mime: str = "application/octet-stream"
    encoding: Literal["utf-8", "base64", "json"] = "base64"
    size: int | None = None
    digest: DigestInfo | None = None

    @field_validator('topic')
    @classmethod
    def validate_topic(cls, v: str) -> str:
        if not _TOPIC_RE.match(v):
            raise ValueError(f"invalid topic name: {v!r} (must match {_TOPIC_RE.pattern})")
        return v


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


class ClipWatchResult(BaseModel):
    topics: list[str]


class TopicsListParams(BaseModel):
    pass


class TopicsListResult(BaseModel):
    topics: list[str]


class HealthResult(BaseModel):
    ok: bool
    xsel_enabled: bool = False
    xsel_good: bool = False


class StatusResult(BaseModel):
    ok: bool
    topics: list[str] = Field(default_factory=list)
    clients: list[str] = Field(default_factory=list)
    xsel: dict[str, JsonValue] = Field(default_factory=dict)


class InternalTopicData(ABC):
    def __init__(self, data: TopicData, source: Interface | None):
        self.data: TopicData = data
        self.source: Interface | None = source

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
