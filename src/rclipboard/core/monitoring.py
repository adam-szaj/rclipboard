"""Monitoring: telemetry data structures, event bus and the MonitorHub."""
import asyncio
import time as _time
from dataclasses import dataclass, field
from enum import Enum

from rclipboard.core.interfaces import Interface
from rclipboard.core.topics import InternalTopicData
from rclipboard.timeutil import utc_timestamp


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
    # Classification is polymorphic: each transport declares monitor_kind and
    # (optionally) monitor_addr on its Interface subclass, so no transport
    # imports are needed here.
    kind = client.monitor_kind
    addr = client.monitor_addr()
    conn_id = f"{kind}:{addr}" if addr else f"{kind}:{id(client):x}"
    return ClientInfo(
        conn_id=conn_id,
        kind=kind,
        addr=addr,
        app=None,
        connected_at=_time.monotonic(),
    )


class MonitorHub:
    """Owner of all monitoring state and monitor-event emission.

    Pure telemetry: AppState calls the hooks below at its mutation points;
    nothing here influences topic storage, dispatch or conflict resolution.
    Events go to bounded per-subscriber queues — a slow consumer drops
    events, it never blocks the caller.
    """

    def __init__(self):
        self.client_info: dict[Interface, ClientInfo] = {}
        self.topic_meta: dict[str, TopicMeta] = {}
        self._subs: set[asyncio.Queue[MonitorEvent]] = set()

    # ── Event bus ────────────────────────────────────────────────────────────

    def subscribe(self) -> "asyncio.Queue[MonitorEvent]":
        q: asyncio.Queue[MonitorEvent] = asyncio.Queue(maxsize=200)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: "asyncio.Queue[MonitorEvent]") -> None:
        self._subs.discard(q)

    def emit(self, event: MonitorEvent) -> None:
        for q in self._subs:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # slow consumer drops events, never block the dispatcher

    async def drain(self, timeout: float = 1.0) -> None:
        """Briefly wait (bounded) until all subscriber queues are consumed."""
        deadline = asyncio.get_event_loop().time() + timeout
        while any(not q.empty() for q in self._subs):
            if asyncio.get_event_loop().time() >= deadline:
                break
            await asyncio.sleep(0.01)

    # ── Lifecycle hooks (called by AppState) ─────────────────────────────────

    def client_connected(self, client: Interface) -> None:
        ci = _make_client_info(client)
        self.client_info[client] = ci
        self.emit(MonitorEvent(
            kind=MonitorEventKind.CLIENT_CONNECTED,
            ts=_time.monotonic(),
            ts_utc=utc_timestamp(),
            conn_id=ci.conn_id,
            data={"kind": ci.kind, "addr": ci.addr},
        ))

    def client_disconnected(self, client: Interface) -> None:
        ci = self.client_info.pop(client, None)
        # remove from notified_clients in all topic_meta
        if ci:
            for tm in self.topic_meta.values():
                tm.notified_clients.pop(ci.conn_id, None)
        self.emit(MonitorEvent(
            kind=MonitorEventKind.CLIENT_DISCONNECTED,
            ts=_time.monotonic(),
            ts_utc=utc_timestamp(),
            conn_id=ci.conn_id if ci else None,
            data={},
        ))

    def client_subscribed(self, client: Interface, topics: list[str]) -> None:
        ci = self.client_info.get(client)
        if ci:
            ci.topics.update(topics)
        self.emit(MonitorEvent(
            kind=MonitorEventKind.CLIENT_SUBSCRIBED,
            ts=asyncio.get_event_loop().time(),
            ts_utc=utc_timestamp(),
            conn_id=ci.conn_id if ci else None,
            data={"topics": list(topics)},
        ))

    def client_unsubscribed(self, client: Interface, topics: list[str]) -> None:
        ci = self.client_info.get(client)
        if ci:
            ci.topics.difference_update(topics)

    def service_stop(self, reason: str, ts_utc: str) -> None:
        self.emit(MonitorEvent(
            kind=MonitorEventKind.SERVICE_STOP,
            ts=asyncio.get_event_loop().time(),
            ts_utc=ts_utc,
            conn_id=None,
            data={"reason": reason},
        ))

    # ── Traffic accounting hooks ─────────────────────────────────────────────

    def topic_notified(self, conn: Interface, topic: str, now: float) -> None:
        ci = self.client_info.get(conn)
        if ci:
            ci.notify_count += 1
            ci.last_notify_at = now
        tm = self.topic_meta.get(topic)
        if tm:
            tm.notify_count += 1
            tm.notified_clients[ci.conn_id if ci else repr(conn)] = now
        self.emit(MonitorEvent(
            kind=MonitorEventKind.TOPIC_NOTIFY,
            ts=now,
            ts_utc=utc_timestamp(),
            conn_id=ci.conn_id if ci else None,
            topic=topic,
            data={},
        ))

    def record_put(self, topic_data: InternalTopicData) -> None:
        now = _time.monotonic()
        source = topic_data.source
        ci = self.client_info.get(source) if source else None
        if ci:
            ci.put_count += 1
            ci.last_put_at = now
            if topic_data.data.meta.get("app"):
                ci.app = str(topic_data.data.meta["app"])
        val = topic_data.data.value.value
        size = len(val.encode()) if not topic_data.data.stub else None
        eff_conn_id = ci.conn_id if ci else topic_data.monitor_conn_id
        eff_app = (ci.app if ci else None) or topic_data.monitor_app or \
                  (str(topic_data.data.meta["app"]) if topic_data.data.meta.get("app") else None)
        self.topic_meta[topic_data.topic] = TopicMeta(
            topic=topic_data.topic,
            size=size,
            stored_at=now,
            stored_at_utc=topic_data.stored_at_utc or utc_timestamp(),
            source_id=eff_conn_id,
            source_app=eff_app,
            source_addr=ci.addr if ci else None,
        )
        self.emit(MonitorEvent(
            kind=MonitorEventKind.TOPIC_PUT,
            ts=now,
            ts_utc=utc_timestamp(),
            conn_id=eff_conn_id,
            topic=topic_data.topic,
            data={"size": size, "app": eff_app},
        ))

    def record_get(self, topic: str, content: object,
                   requester: Interface | None) -> None:
        now = _time.monotonic()
        ci = self.client_info.get(requester) if requester else None
        if ci:
            ci.get_count += 1
            ci.last_get_at = now
        tm = self.topic_meta.get(topic)
        if tm and content:
            tm.get_count += 1
            tm.last_get_at = now
            tm.last_get_by = ci.conn_id if ci else None
        if content:
            self.emit(MonitorEvent(
                kind=MonitorEventKind.TOPIC_GET,
                ts=now,
                ts_utc=utc_timestamp(),
                conn_id=ci.conn_id if ci else None,
                topic=topic,
                data={},
            ))
