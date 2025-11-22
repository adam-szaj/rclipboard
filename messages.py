"""
Lightweight helpers for building structured JSON messages.

This module defines helpers used by both HTTP and WebSocket layers to
normalize payloads, validate encodings, and build protocol envelopes.

All helpers return plain dictionaries ready to be JSON-encoded.
"""

from datetime import datetime, timezone
from typing import Any, Iterable
import base64
import itertools
import re


def makeMessage(**kwargs) -> dict:
    """Create a generic message dict from keyword arguments."""
    return kwargs


def utcTimestamp() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


def jsonErr(msg: str):
    """Standard error payload with message and timestamp."""
    return {"type": "error", "message": msg, "ts": utcTimestamp()}


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
    """Validate binary encoding constraints when valueType == 'binary'.

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
        value_type = it.get("valueType")
        value_encoding = it.get("valueEncoding")
        validate_encoding(value_type, value_encoding, value)
        norm.append({
            "topic":
            topic,
            "value":
            value,
            **({
                "valueType": value_type
            } if value_type else {}),
            **({
                "valueEncoding": value_encoding
            } if value_encoding else {}),
        })
    return norm


def make_broadcast_publish(data: dict | list[dict],
                           meta: dict | None = None,
                           ts: Any = None) -> dict:
    """Create a broadcast/publish envelope with DataItem(s)."""
    return makeMessage(
        type="broadcast",
        action="publish",
        data=data,
        meta=meta or {},
        ts=ts or utcTimestamp(),
    )


def makeRequestMessage(**kwargs):
    return makeMessage(type="request", **kwargs)


def makeSubscribeRequest(topics: list[str],
                         ts: Any = None,
                         id: int | None = None) -> dict:
    """Create a subscribe request for given topics."""
    return makeMessage(
        type="system-request",
        id=id or next_id(),
        action="subscribe",
        topics=topics,
        ts=ts or utcTimestamp(),
    )


def makeUnsubscribeRequest(topics: list[str],
                           ts: Any = None,
                           id: int | None = None) -> dict:
    """Create an unsubscribe request for given topics."""
    return makeMessage(
        type="system-request",
        id=id or next_id(),
        action="unsubscribe",
        topics=topics,
        ts=ts or utcTimestamp(),
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
      { "type":"message", "action": "publish", "topic":"a","data":<json>,"ts": }
"""


def makeSystemResponse(request_msg: dict, event: str, **kwargs):
    """Create a system response (ack) derived from a request message."""
    return makeMessage(
        type="system-response",
        event=event,
        id=request_msg.get("id", 0),
        **kwargs,
        ts=utcTimestamp(),
    )


def makeCallRequest(method: str,
                    params: dict | None = None,
                    meta: dict | None = None,
                    id: int | None = None,
                    ts: Any | None = None) -> dict:
    """Create a generic call request with method and params."""
    return makeMessage(
        type="request",
        id=id or next_id(),
        action="call",
        method=method,
        params=params or {},
        meta=meta or {},
        ts=ts or utcTimestamp(),
    )


def makePingRequest(ts: Any = None, id: int | None = None):
    """Create a ping request message."""
    return makeMessage(
        type="system-request",
        id=id or next_id(),
        action="ping",
        ts=utcTimestamp(),
    )


def makeResponse(request_msg: dict, **kwargs) -> dict:
    """Create a response/return wrapper using fields from the request message."""
    return makeMessage(
        type="response",
        event="return",
        id=request_msg.get("id", 0),
        method=request_msg.get("method"),
        **kwargs,
        ts=utcTimestamp(),
    )


def makeErrorResponse(message: str, ts: Any = None) -> dict:
    """Create an error response wrapper."""
    return makeMessage(type="error", message=message, ts=ts or utcTimestamp())


def makeResponseError(request_msg: dict, error: Any) -> dict:
    """Create a response/error envelope based on a request."""
    return makeMessage(
        type="response",
        event="error",
        id=request_msg.get("id", 0),
        method=request_msg.get("method"),
        error=error,
        ts=utcTimestamp(),
    )
