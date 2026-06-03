import itertools
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

from pydantic import JsonValue

from rclipboard.types import ClipboardItem, TopicData


@dataclass(frozen=True)
class EndpointConfig:
    scheme: str
    host: str | None = None
    port: int | None = None
    path: str | None = None


def parse_endpoint(
    raw: str,
    *,
    default_host: str = "127.0.0.1",
    default_port: int = 8989,
) -> EndpointConfig:
    raw = raw.strip()
    if not raw:
        raise ValueError("endpoint must not be empty")

    if "://" in raw:
        parsed = urlparse(raw)
        scheme = parsed.scheme
        if scheme in {"http", "https", "ws", "wss"}:
            return EndpointConfig(
                scheme=scheme,
                host=parsed.hostname or default_host,
                port=parsed.port or default_port,
            )
        if scheme == "uds":
            path = parsed.path or parsed.netloc
            if not path:
                raise ValueError(f"{scheme} endpoint requires a path")
            return EndpointConfig(scheme=scheme, path=path)
        raise ValueError(f"unsupported endpoint scheme: {scheme}")

    if raw.startswith("/"):
        return EndpointConfig(scheme="uds", path=raw)

    if raw.isdigit():
        return EndpointConfig(scheme="http", host=default_host, port=int(raw))

    if ":" in raw:
        host, port_raw = raw.rsplit(":", 1)
        if not port_raw.isdigit():
            raise ValueError(f"invalid endpoint port: {raw}")
        return EndpointConfig(scheme="http",
                              host=host or default_host,
                              port=int(port_raw))

    return EndpointConfig(scheme="http", host=raw, port=default_port)


def bind_endpoint_from_env() -> EndpointConfig:
    endpoint = os.environ.get("RCLIPBOARD_ENDPOINT")
    if endpoint:
        return parse_endpoint(endpoint)

    uds = os.environ.get("RCLIPBOARD_BIND_UDS")
    if uds:
        return EndpointConfig(scheme="uds", path=uds)

    host = os.environ.get("RCLIPBOARD_BIND_ADDR", "127.0.0.1")
    port = int(os.environ.get("RCLIPBOARD_BIND_PORT", 8989))
    return EndpointConfig(scheme="http", host=host, port=port)


def upstream_endpoint_from_env() -> EndpointConfig:
    host = os.environ.get("RCLIPBOARD_UPSTREAM_ADDR", "127.0.0.1")
    port = int(os.environ.get("RCLIPBOARD_UPSTREAM_PORT", 8989))
    uds = os.environ.get("RCLIPBOARD_UPSTREAM_UDS")
    endpoint = os.environ.get("RCLIPBOARD_UPSTREAM_ENDPOINT")
    if endpoint:
        return parse_endpoint(endpoint)

    if uds:
        return EndpointConfig(scheme="uds", path=uds, host=host, port=port)

    return EndpointConfig(scheme="http", host=host, port=port)


def topic_data_to_clipboard_item(data: TopicData) -> ClipboardItem:
    value = data.value.value
    value_type = data.value.value_type
    encoding = data.value.value_encoding
    mime = "application/octet-stream" if value_type == "binary" else "text/plain"
    rpc_encoding = "utf-8" if encoding == "plain" else encoding or "base64"
    encrypted = data.meta.get("encrypted", False) is True
    return ClipboardItem(
        topic=data.topic,
        value=value,
        mime=mime,
        encoding=rpc_encoding,
        encrypted=encrypted,
    )


def clipboard_item_to_topic_data(
        item: ClipboardItem,
        meta: dict[str, JsonValue] | None = None) -> TopicData:
    value_type = "binary"
    value_encoding = item.encoding
    if item.encoding == "utf-8":
        value_type = "text"
        value_encoding = "plain"
    combined_meta = dict(meta or {})
    if item.encrypted:
        combined_meta["encrypted"] = True
    if "ts" not in combined_meta:
        combined_meta["ts"] = utc_timestamp()
    return TopicData.model_validate({
        "topic": item.topic,
        "meta": combined_meta,
        "value": {
            "value": item.value,
            "type": value_type,
            "encoding": value_encoding,
        },
    })


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_utc_timestamp(ts: object) -> datetime | None:
    """Parse an ISO-8601 UTC timestamp into an aware datetime.

    Returns ``None`` for missing/empty/unparseable values so callers can treat
    "no comparable timestamp" uniformly. A naive datetime (no tzinfo) is
    assumed to be UTC.
    """
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


_id_counter = itertools.count(1)


def next_id() -> int:
    return next(_id_counter)
