from pydantic import BaseModel, Field, JsonValue
from pydantic import ConfigDict

# from typing import Literal
from typing import Annotated, Literal, Required, TypeVar, Generic
from abc import ABC, abstractmethod
import asyncio as a
from asyncio import Future, Transport


class ValueData(BaseModel):
    value: str
    value_type: Literal["text", "binary", "path", "file"] = Field(
        alias="type", default="text"
    )
    value_encoding: Literal["plain", "hex", "base64"] = Field(
        alias="encoding", default="plain"
    )


class TopicData(BaseModel):
    topic: Annotated[str, Field(init=True)]
    value: Annotated[ValueData, Field(init=True)]
    meta: Annotated[dict[str, str], Field(init=True)]

    model_config = ConfigDict(arbitrary_types_allowed=True)


class Connection(ABC):
    def __init__(self):
        pass

    @abstractmethod
    async def enqueue_topic_data(self, data: "TopicData"):
        pass

    @abstractmethod
    async def send(self, data: dict[str, object]):
        pass

    def __repr__(self) -> str:
        return "conn"

    def __str__(self) -> str:
        return "conn"


class InternalTopicData(ABC):
    def __init__(self, data: TopicData, source: Connection | None):
        self.data: TopicData = data
        self.source: Connection|None = source

    @property
    def topic(self) -> str:
        return self.data.topic


PT = TypeVar("PT")
RT = TypeVar("RT")


class AppRequestMessage(Generic[PT, RT]):
    def __init__(self, loop: a.AbstractEventLoop, action: str, data: PT) -> None:
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
