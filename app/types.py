from .app_state import Connection
from pydantic import BaseModel, JsonValue
from typing import Literal
# from typing import Annotated, Literal


class TopicData(BaseModel):
    topic: str
    source: Connection
    value: dict[str, str]
    meta: dict[str, str]


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
