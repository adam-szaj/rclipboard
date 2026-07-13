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


def response_payload(request_id: RPCId, *,
                     result: JsonValue | None = None,
                     error: RPCError | None = None) -> dict:
    """Build the JSON-RPC response envelope dict shared by WS and raw UDS.

    Only the dict construction is shared — each transport keeps its own
    serialization/framing (starlette ``send_json`` vs ``json.dumps`` + newline)
    so the wire bytes stay exactly as before.
    """
    return JSONRPCResponseMessage(
        **{"id": request_id},
        jsonrpc="2.0",
        result=result,
        error=error,
    ).model_dump(by_alias=True, mode="json", exclude_none=True)


def notification_payload(method: str, params: object) -> dict:
    """Build the JSON-RPC notification envelope dict (no id, fire-and-forget)."""
    return {"jsonrpc": "2.0", "method": method, "params": params}
