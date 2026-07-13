"""Conversions between the wire ClipboardItem and the internal TopicData."""
from pydantic import JsonValue

from rclipboard.models.wire import ClipboardItem, TopicData
from rclipboard.timeutil import utc_timestamp


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
