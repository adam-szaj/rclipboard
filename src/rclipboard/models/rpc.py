"""JSON-RPC 2.0 envelope models shared by WS, raw UDS and the proxy client."""
import itertools
from typing import Annotated, Literal

from pydantic import BaseModel, Field, JsonValue

from rclipboard.models.wire import RPCError

RPCId = int | str | None

_id_counter = itertools.count(1)


def next_id() -> int:
    return next(_id_counter)


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
