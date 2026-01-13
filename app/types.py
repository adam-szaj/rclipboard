import asyncio as a
from abc import ABC, abstractmethod
from typing import Annotated, Generic, Literal, TypeVar, override

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class Connection(ABC):

    def __init__(self):
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @abstractmethod
    async def send(self, data: object):
        pass

    @override
    def __repr__(self) -> str:
        return f"Connection[{self.name}]"


class ValueData(BaseModel):
    value: str
    value_type: Literal["text", "binary", "path",
                        "file"] = Field(alias="type", default="text")
    value_encoding: Literal["plain", "hex", "base64"] = Field(alias="encoding",
                                                              default="plain")


class TopicData(BaseModel):
    topic: Annotated[str, Field(init=True)]
    value: Annotated[ValueData, Field(init=True)]
    meta: Annotated[dict[str, str], Field(init=True)]

    model_config = ConfigDict(arbitrary_types_allowed=True)


class Health(BaseModel):
    ok: bool
    http_enabled: bool
    http_good: bool
    xsel_enabled: bool
    xsel_good: bool
    ws_enabled: bool
    ws_good: bool
    uds_enabled: bool
    uds_good: bool
    pass


class HTTPStatus(BaseModel):
    enabled: bool
    good: bool
    bind_address: str
    bind_port: int


class WSStatus(BaseModel):
    enabled: bool
    good: bool
    bind_address: str
    bind_port: int
    endpoint: str


class XselStatus(BaseModel):
    enabled: bool
    good: bool


class StatusData(BaseModel):
    health: Health
    http_status: HTTPStatus
    ws_status: WSStatus
    xsel_status: XselStatus


class InternalTopicData(ABC):

    def __init__(self, data: TopicData, source: Connection | None):
        self.data: TopicData = data
        self.source: Connection | None = source

    @property
    def topic(self) -> str:
        return self.data.topic


PT = TypeVar("PT")
RT = TypeVar("RT")


class AppRequestMessage(Generic[PT, RT]):

    def __init__(self, loop: a.AbstractEventLoop, action: str,
                 data: PT) -> None:
        self._action: str = action
        self._future: a.Future[RT] = loop.create_future()
        self.data: PT = data

    def future(self) -> a.Future[RT]:
        return self._future


class Message(BaseModel):
    pass


class BroadcastMessage(Message):
    type: Literal["broadcast"]
    action: Literal["event"]
    method: Literal["clip"]
    value: TopicData


class RequestMessage(Message):
    type: Literal["request"] = "request"
    action: Literal["call"] = "call"
    mid: Annotated[int, Field(ge=0, default=0, alias="id")] = 0
    method: Literal["clip", "getclip", "status", "health"]
    params: JsonValue


class ResponseMessage(Message):
    type: Literal["response"] = "response"
    action: Literal["return"] = "return"
    mid: Annotated[int, Field(ge=0, default=0, alias="id")] = 0
    method: str
    value: JsonValue


class ErrorMessage(Message):
    type: Literal["response"] = "response"
    action: Literal["error"] = "error"
    mid: Annotated[int, Field(ge=0, default=0, alias="id")] = 0
    error: JsonValue


class SystemRequestMessage(Message):
    type: Literal["system-request"]
    action: Literal["call"]
    method: Literal["subscribe", "unsubscribe"]
    params: JsonValue


class SystemResponseMessage(Message):
    type: Literal["system-response"]
    action: Literal["return", "error"]
    event: Literal["subscribed", "unsubscribed"]
    value: JsonValue

    # data: TopicData
