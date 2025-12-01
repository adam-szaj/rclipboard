"""
Lightweight helpers for building structured JSON messages.

This module defines helpers used by both HTTP and WebSocket layers to
normalize payloads, validate encodings, and build protocol envelopes.

All helpers return plain dictionaries ready to be JSON-encoded.
"""

from datetime import datetime, timezone
from typing import Any
import base64
import itertools
import re


def make_dict(**kwargs) -> dict[str, object]:
    """Create a generic message dict from keyword arguments."""
    return kwargs


def makeMessage(**kwargs) -> dict[str, object]:
    return make_dict(**kwargs)


def utc_timestamp() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


def jsonErr(msg: str):
    """Standard error payload with message and timestamp."""
    return {"type": "error", "message": msg, "ts": utc_timestamp()}


# -------------------- Id generator --------------------

_id_counter = itertools.count(1)


def next_id() -> int:
    """Return a monotonically increasing request id."""
    return next(_id_counter)


# -------------------- DataItem helpers --------------------

_B64_RE = re.compile(r"^[A-Za-z0-9+/=]+$")
_HEX_RE = re.compile(r"^[0-9a-f]+$")


def validate_encoding(value_type: str | None, value_encoding: str | None,
                      value: Any):
    """Validate binary encoding constraints when type == 'binary'.

    - hex: lowercase hex, no separators, no 0x
    - base64: RFC 4648 alphabet (padding allowed)
    """
    if value_type != "binary":
        return
    if not isinstance(value, str):
        raise ValueError("binary value must be a string")
    if value_encoding not in ("base64", "hex"):
        raise ValueError("valueEncoding must be 'base64' or 'hex'")
    if value_encoding == "hex" and not _HEX_RE.fullmatch(value):
        raise ValueError("hex must be lowercase, no 0x and no separators")
    if value_encoding == "base64":
        if value and not _B64_RE.fullmatch(value):
            raise ValueError("invalid base64 characters")
        try:
            base64.b64decode(value, validate=True)
        except Exception as e:
            raise ValueError("invalid base64") from e


def normalize_data_items(obj: Any,
                         topic_fallback: str | None = None) -> list[dict]:
    """Normalize input into a list of DataItem dicts.

    Accepts either a single dict or a list of dicts. Ensures required keys
    and validates encoding rules for binary payloads.
    """
    if obj is None:
        raise ValueError("data is required")
    items: Iterable[Any]
    if isinstance(obj, list):
        items = obj
    elif isinstance(obj, dict):
        items = [obj]
    else:
        raise ValueError("data must be an object or array of objects")

    norm: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            raise ValueError("each data item must be an object")
        topic = it.get("topic") or topic_fallback
        if not topic:
            raise ValueError("data.topic is required")
        value = it.get("value")
        value_type = it.get("type", "binary")
        value_encoding = it.get("encoding", "base64")

        validate_encoding(value_type, value_encoding, value)

        norm.append({
            "topic": topic,
            "value": value,
            "type": value_type,
            "encoding": value_encoding,
        })
    return norm


def makeRequestMessage(**kwargs):
    return make_dict(type="request", **kwargs)


def makeSubscribeRequest(topics: list[str],
                         ts: Any = None,
                         id: int | None = None) -> dict:
    """Create a subscribe request for given topics."""
    return make_dict(
        type="system-request",
        id=id or next_id(),
        action="subscribe",
        topics=topics,
        ts=ts or utc_timestamp(),
    )


def makeUnsubscribeRequest(topics: list[str],
                           ts: Any = None,
                           id: int | None = None) -> dict:
    """Create an unsubscribe request for given topics."""
    return make_dict(
        type="system-request",
        id=id or next_id(),
        action="unsubscribe",
        topics=topics,
        ts=ts or utc_timestamp(),
    )


"""
WebSocket protocol (client -> server):
  { "type": "request", "action": "subscribe",   "topics": ["a","b", ...] }
  { "type": "request", "action": "unsubscribe", "topics": ["a","b", ...] }
  { "type": "request", "action": "request",     "method": "get", params: {"topic": ["a", "b"]}}
  { "type": "request", "action": "ping" }

Server -> client payloads:
  System acks/errors:
    { "type":"system", "event": "subscribed","topics":[...], "ts": ... }
    { "type":"system", "event": "unsubscribed","topics":[...], "ts": ... }
    { "type":"response", "event": "return", "method": "get", "value": {}}
    { "type":"response", "event": "error", "method": "get", "error": {}}

  Broadcast messages:
      { "type":"message", "action": "clip", "topic":"a","data":<json>,"ts": }
"""


def makeSystemResponse(request_msg: dict, event: str, **kwargs):
    """Create a system response (ack) derived from a request message."""
    return make_dict(
        type="system-response",
        event=event,
        id=request_msg.get("id", 0),
        **kwargs,
        ts=utc_timestamp(),
    )


def makeCallRequest(method: str,
                    params: dict | None = None,
                    meta: dict | None = None,
                    id: int | None = None,
                    ts: Any | None = None) -> dict:
    """Create a generic call request with method and params."""
    return make_dict(
        type="request",
        id=id or next_id(),
        action="call",
        method=method,
        params=params or {},
        meta=meta or {},
        ts=ts or utc_timestamp(),
    )


def makePingRequest(ts: Any = None, id: int | None = None):
    """Create a ping request message."""
    return make_dict(
        type="system-request",
        id=id or next_id(),
        action="ping",
        ts=utc_timestamp(),
    )


def makeResponse(request_msg: dict, **kwargs) -> dict:
    """Create a response/return wrapper using fields from the request message."""
    return make_dict(
        type="response",
        event="return",
        id=request_msg.get("id", 0),
        method=request_msg.get("method"),
        **kwargs,
        ts=utc_timestamp(),
    )


def makeErrorResponse(message: str, ts: Any = None) -> dict:
    """Create an error response wrapper."""
    return make_dict(type="error", message=message, ts=ts or utc_timestamp())


def makeResponseError(request_msg: dict, error: Any) -> dict:
    """Create a response/error envelope based on a request."""
    return make_dict(
        type="response",
        event="error",
        id=request_msg.get("id", 0),
        method=request_msg.get("method"),
        error=error,
        ts=utc_timestamp(),
    )
