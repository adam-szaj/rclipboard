"""Monitoring data structures (client/topic telemetry and monitor events)."""
import time as _time
from dataclasses import dataclass, field
from enum import Enum

from rclipboard.core.interfaces import Interface


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


def _make_client_info(client: Interface) -> ClientInfo:
    # Lazy imports + class-name string checks: transports import core, so a
    # module-scope import here would be circular (kept as-is on purpose).
    from rclipboard.transports.proxy import ProxyClient
    from rclipboard.transports.xsel import XselInterface
    if isinstance(client, ProxyClient):
        kind = "proxy"
        addr = client.url if hasattr(client, "url") else None
    elif isinstance(client, XselInterface):
        kind = "xsel"
        addr = None
    elif client.__class__.__name__ == "WSServerConnection":
        kind = "ws"
        ws = getattr(client, "ws", None)
        addr = (f"{ws.client.host}:{ws.client.port}"
                if ws and ws.client else None)
    elif client.__class__.__name__ == "UDSServerConnection":
        kind = "uds"
        addr = None
    else:
        kind = "unknown"
        addr = None
    conn_id = f"{kind}:{addr}" if addr else f"{kind}:{id(client):x}"
    return ClientInfo(
        conn_id=conn_id,
        kind=kind,
        addr=addr,
        app=None,
        connected_at=_time.monotonic(),
    )
