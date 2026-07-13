"""Monitoring data structures (client/topic telemetry and monitor events)."""
from dataclasses import dataclass, field
from enum import Enum


@dataclass
class ClientInfo:
    conn_id: str
    kind: str                           # "ws" | "uds" | "proxy" | "xsel"
    addr: str | None
    app: str | None
    connected_at: float                 # monotonic
    topics: set[str] = field(default_factory=set)
    put_count: int = 0
    get_count: int = 0
    notify_count: int = 0
    last_put_at: float | None = None
    last_get_at: float | None = None
    last_notify_at: float | None = None


@dataclass
class TopicMeta:
    topic: str
    size: int | None
    stored_at: float                    # monotonic
    stored_at_utc: str
    source_id: str | None
    source_app: str | None
    source_addr: str | None
    get_count: int = 0
    notify_count: int = 0
    notified_clients: dict[str, float] = field(default_factory=dict)  # conn_id → last_notify_at
    last_get_at: float | None = None
    last_get_by: str | None = None


class MonitorEventKind(str, Enum):
    CLIENT_CONNECTED    = "client.connected"
    CLIENT_DISCONNECTED = "client.disconnected"
    CLIENT_SUBSCRIBED   = "client.subscribed"
    TOPIC_PUT           = "topic.put"
    TOPIC_GET           = "topic.get"
    TOPIC_NOTIFY        = "topic.notify"
    PROXY_CONNECTED     = "proxy.connected"
    PROXY_DISCONNECTED  = "proxy.disconnected"
    SERVICE_STOP        = "service.stop"


@dataclass
class MonitorEvent:
    kind: MonitorEventKind
    ts: float                           # monotonic
    ts_utc: str
    conn_id: str | None = None
    topic: str | None = None
    data: dict = field(default_factory=dict)
