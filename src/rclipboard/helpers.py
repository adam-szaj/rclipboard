"""Compatibility shim — contents moved to endpoints/timeutil/models.

Kept during the module-structure refactor so existing imports keep working;
scheduled for removal once all importers point at the new locations.
"""
from rclipboard.endpoints import (
    EndpointConfig,
    bind_endpoint_from_env,
    parse_endpoint,
    upstream_endpoint_from_env,
)
from rclipboard.models.convert import (
    clipboard_item_to_topic_data,
    topic_data_to_clipboard_item,
)
from rclipboard.models.rpc import next_id
from rclipboard.timeutil import parse_utc_timestamp, utc_timestamp

__all__ = [
    "EndpointConfig",
    "bind_endpoint_from_env",
    "clipboard_item_to_topic_data",
    "next_id",
    "parse_endpoint",
    "parse_utc_timestamp",
    "topic_data_to_clipboard_item",
    "upstream_endpoint_from_env",
    "utc_timestamp",
]
