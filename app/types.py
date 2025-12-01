from pydantic import BaseModel, ConfigDict, Field, JsonValue
from typing import Literal
# from typing import Annotated, Literal
from abc import ABC, abstractmethod


class ValueData(BaseModel):
    value: str
    value_type: str = Field(alias='type')
    value_encoding: Literal['plain', 'hex', 'base64'] = Field(alias='encoding')


class TopicData(BaseModel):
    topic: str
    source: str
    value: ValueData
    meta: dict[str, str]

    # model_config = ConfigDict(arbitrary_types_allowed=True)


class Connection(ABC):

    def __init__(self):
        pass

    @abstractmethod
    async def enqueue_topic_data(self, data: TopicData):
        pass

    @abstractmethod
    async def send(self, data: dict[str, object]):
        pass

    def __repr__(self) -> str:
        return "conn"

    def __str__(self) -> str:
        return "conn"


# class SerialFlowMessage(BaseModel):


class Message(BaseModel):
    pass


class BroadcastMessage(Message):
    type: Literal['broadcast']
    action: Literal['event']
    method: Literal['clip']
    value: TopicData


class RequestMessage(Message):
    type: Literal['request']
    action: Literal['call']
    method: Literal['clip', 'getclip', 'status', 'health']
    params: JsonValue


class ResponseMessage(Message):
    type: Literal['response']
    action: Literal['return', 'error']
    value: JsonValue


class SystemRequestMessage(Message):
    type: Literal['system-request']
    action: Literal['call']
    method: Literal['subscribe', 'unsubscribe']
    params: JsonValue


class SystemResponseMessage(Message):
    type: Literal['system-response']
    action: Literal['return', 'error']
    event: Literal['subscribed', 'unsubscribed']
    value: JsonValue

    # data: TopicData
