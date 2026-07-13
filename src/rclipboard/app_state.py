"""Compatibility shim — contents moved to rclipboard.core.

Kept during the module-structure refactor so existing imports keep working;
scheduled for removal once all importers point at the new locations.
"""
from rclipboard.core.bus import (
    Bus,
    GenericSerialCall,
    GetTopicData,
    ItemType,
    SerialCall,
    SetTopicData,
)
from rclipboard.core.monitoring import _make_client_info
from rclipboard.core.state import (
    AppState,
    _resolve_compare_ts,
    enqueue_request_topic,
    enqueue_request_topics,
    enqueue_topic_data,
    enqueue_topic_data_nowait,
    register_client,
    subscribe_client,
    unregister_client,
    unsubscribe_client,
)

__all__ = [
    "AppState",
    "Bus",
    "GenericSerialCall",
    "GetTopicData",
    "ItemType",
    "SerialCall",
    "SetTopicData",
    "_make_client_info",
    "_resolve_compare_ts",
    "enqueue_request_topic",
    "enqueue_request_topics",
    "enqueue_topic_data",
    "enqueue_topic_data_nowait",
    "register_client",
    "subscribe_client",
    "unregister_client",
    "unsubscribe_client",
]
