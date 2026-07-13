import asyncio
import contextlib
import datetime
import os
import time as _time
from abc import ABC, abstractmethod
from logging import Logger
from typing import Any, Generic, TypeVar, override

from fastapi import FastAPI

from rclipboard.log import get_logger
from rclipboard.types import (
    BidirectionalInterface,
    ClientInfo,
    HealthResult,
    Interface,
    InternalTopicData,
    MonitorEvent,
    MonitorEventKind,
    StatusResult,
    TopicData,
    TopicMeta,
    TopicStatus,
    ValueData,
)

logger: Logger = get_logger(__name__)
error = logger.error
warning = logger.warning
info = logger.info
debug = logger.debug

T = TypeVar("T")
ItemType = InternalTopicData | list[str] | dict[str, str] | None


class Bus(Generic[T]):

    def __init__(self, **kwargs):
        self.q: asyncio.Queue[T] = asyncio.Queue[T](**kwargs)

    def task_done(self):
        self.q.task_done()

    async def get_nowait(self):
        return self.q.get_nowait()

    async def get(self):
        return await self.q.get()

    async def put(self, data: T):
        return await self.q.put(data)

    async def request(self, request: T):
        pass

    async def put_nowait(self, data: T):
        return self.q.put_nowait(data)


class SerialCall(ABC, Generic[T]):

    def __init__(self, future: asyncio.Future[T] | None = None):
        if future is None:
            future = asyncio.Future()
        self._future: asyncio.Future[T] = future

    @property
    def future(self) -> asyncio.Future[T]:
        return self._future

    @abstractmethod
    async def do_call(self, app: "AppState") -> T:
        pass

    async def call(self, app: "AppState"):
        try:
            result: T = await self.do_call(app)
            self._future.set_result(result)
        except Exception as e:
            self._future.set_exception(e)


class SetTopicData(SerialCall[None]):

    def __init__(
        self,
        topic_data: InternalTopicData,
        future: asyncio.Future[None] | None,
    ):
        super().__init__(future)
        self.topic_data: InternalTopicData = topic_data

    @override
    async def do_call(self, app: "AppState") -> None:
        await app.process_put_item(self.topic_data)


class GetTopicData(SerialCall[InternalTopicData | None]):

    def __init__(
        self,
        topic: str,
        future: asyncio.Future[InternalTopicData | None] | None,
        requester: "Interface | None" = None,
    ):
        super().__init__(future)
        self.topic: str = topic
        self.requester: "Interface | None" = requester

    @override
    async def do_call(self, app: "AppState") -> InternalTopicData | None:
        item: ItemType = await app.process_get_item("topic", self.topic,
                                                    self.requester)
        if item is None:
            return None
        assert isinstance(item, InternalTopicData)
        return item


GenericSerialCall = GetTopicData | SetTopicData


class AppState:

    def __init__(self, app: FastAPI):
        self.app: FastAPI = app
        self.bus: Bus[GenericSerialCall] = Bus[GenericSerialCall]()
        self.clients: list[Interface] = []
        self.topic_content: dict[str, InternalTopicData] = {}
        self.subs: dict[str, set[Interface]] = {}
        self.notify_delay_ms: int = int(
            os.environ.get("RCLIPBOARD_NOTIFY_DELAY_MS", "250"))
        self.lazy_local_threshold: int = (
            int(os.environ.get("RCLIPBOARD_LAZY_LOCAL_KB", "0")) * 1024)
        # Tolerance for the time-based conflict resolution. Timestamps within
        # this window are treated as a tie (→ local wins), which absorbs the
        # residual error of the clock-offset estimate (~RTT/2) and minor clock
        # skew, while genuinely newer values still win outside the window.
        # Default 100 ms: comfortably above localhost RTT/2, yet far below the
        # gap between two human clipboard actions, so real updates aren't masked.
        self.sync_tie_window: datetime.timedelta = datetime.timedelta(
            milliseconds=int(os.environ.get("RCLIPBOARD_SYNC_TIE_MS", "100")))
        self.pending_notifications: dict[str, InternalTopicData] = {}
        self.notification_tasks: dict[str, asyncio.Task[None]] = {}
        self.notification_lock = asyncio.Lock()
        self.public_keys: dict[str, dict] = {}  # age1pubkey → {public_key, label, key_id}
        self._background_tasks: set[asyncio.Task] = set()
        # monitoring
        self.client_info: dict[Interface, ClientInfo] = {}
        self.topic_meta: dict[str, TopicMeta] = {}
        self._monitor_subs: set[asyncio.Queue[MonitorEvent]] = set()
        self.dispatcher_task: asyncio.Task[None] = asyncio.create_task(
            self.dispatcher(), name="dispatcher")

    # ── Monitor event bus ─────────────────────────────────────────────────────

    def subscribe_monitor(self) -> "asyncio.Queue[MonitorEvent]":
        q: asyncio.Queue[MonitorEvent] = asyncio.Queue(maxsize=200)
        self._monitor_subs.add(q)
        return q

    def unsubscribe_monitor(self, q: "asyncio.Queue[MonitorEvent]") -> None:
        self._monitor_subs.discard(q)

    def _emit_monitor(self, event: MonitorEvent) -> None:
        for q in self._monitor_subs:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # slow consumer drops events, never block the dispatcher

    @staticmethod
    def _utcnow() -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    async def broadcast_shutdown(self, reason: str = "server shutting down") -> None:
        """Tell every connected client/upstream we are stopping.

        Sends a fire-and-forget ``server.shutdown`` JSON-RPC notification to all
        RPC-capable clients (WS, raw UDS) and to the proxy upstream, and emits a
        ``service.stop`` monitor event. Called from ``_RclipboardServer.shutdown``
        (rclipboard/__init__.py) *before* uvicorn closes the connections, so the
        notice lands while the sockets are still alive.

        RPC notices are written straight to each socket. Monitor events go through
        the per-subscriber queue, so we briefly wait for those queues to drain
        (bounded) — otherwise uvicorn's connection-close (1012) could race ahead of
        the ``service.stop`` delivery.
        """
        # Lazy import: rpc_handler imports app_state, so importing at module
        # scope would be circular.
        from rclipboard.rpc_handler import RPCHandler
        ts = self._utcnow()
        params = {"reason": reason, "ts_utc": ts}
        for client in list(self.clients):  # snapshot — sending may mutate clients
            if isinstance(client, RPCHandler):
                try:
                    await client._send_event("server.shutdown", params)
                except Exception:
                    pass  # dead/closed connection — ignore during shutdown
        self._emit_monitor(MonitorEvent(
            kind=MonitorEventKind.SERVICE_STOP,
            ts=asyncio.get_event_loop().time(),
            ts_utc=ts,
            conn_id=None,
            data={"reason": reason},
        ))
        # Let monitor handlers consume + flush their queues (and self-close) before
        # the caller proceeds to uvicorn's connection teardown. Bounded so a stuck
        # consumer can't delay shutdown.
        deadline = asyncio.get_event_loop().time() + 1.0
        while any(not q.empty() for q in self._monitor_subs):
            if asyncio.get_event_loop().time() >= deadline:
                break
            await asyncio.sleep(0.01)

    # ── Client lifecycle ──────────────────────────────────────────────────────

    def subscribe_client(self, client: Interface,
                         topics: list[str]) -> dict[str, TopicData]:
        contents: dict[str, TopicData] = dict()
        for topic in topics:
            if topic not in self.subs:
                self.subs[topic] = set()
            self.subs[topic].add(client)
            content = self.topic_content.get(topic)
            if content:
                contents[topic] = content.data
        ci = self.client_info.get(client)
        if ci:
            ci.topics.update(topics)
        self._emit_monitor(MonitorEvent(
            kind=MonitorEventKind.CLIENT_SUBSCRIBED,
            ts=asyncio.get_event_loop().time(),
            ts_utc=self._utcnow(),
            conn_id=ci.conn_id if ci else None,
            data={"topics": list(topics)},
        ))
        self._emit_runtime_state_change("subscriptions")
        return contents

    def unsubscribe_client(self, client: Interface, topics: list[str]):
        for topic in topics:
            subs = self.subs.get(topic)
            if not subs:
                continue
            subs.discard(client)
            if not subs:
                del self.subs[topic]
        ci = self.client_info.get(client)
        if ci:
            ci.topics.difference_update(topics)
        self._emit_runtime_state_change("subscriptions")

    def register_client(self, client: Interface):
        self.clients.append(client)
        if isinstance(client, BidirectionalInterface):
            client.start_drainer()
        ci = _make_client_info(client)
        self.client_info[client] = ci
        self._emit_monitor(MonitorEvent(
            kind=MonitorEventKind.CLIENT_CONNECTED,
            ts=_time.monotonic(),
            ts_utc=self._utcnow(),
            conn_id=ci.conn_id,
            data={"kind": ci.kind, "addr": ci.addr},
        ))
        self._emit_runtime_state_change("clients")

    def unregister_client(self, client: Interface):
        self.clients.remove(client)
        ci = self.client_info.pop(client, None)
        # remove from notified_clients in all topic_meta
        if ci:
            for tm in self.topic_meta.values():
                tm.notified_clients.pop(ci.conn_id, None)
        self._emit_monitor(MonitorEvent(
            kind=MonitorEventKind.CLIENT_DISCONNECTED,
            ts=_time.monotonic(),
            ts_utc=self._utcnow(),
            conn_id=ci.conn_id if ci else None,
            data={},
        ))
        self._emit_runtime_state_change("clients")

    def get_health(self) -> HealthResult:
        from rclipboard.proxy import get_proxy_status
        from rclipboard.xsel import get_xsel_status
        xsel = get_xsel_status(self.app)
        proxy = get_proxy_status(self.app)
        return HealthResult(
            ok=True,
            xsel_enabled=bool(xsel["enabled"]),
            xsel_good=bool(xsel["good"]),
            proxy_enabled=bool(proxy["enabled"]),
            proxy_good=bool(proxy["good"]),
        )

    def get_status(self, topics: list[str]) -> StatusResult:
        from rclipboard.proxy import get_proxy_status
        from rclipboard.xsel import get_xsel_status
        clients = [c.name for c in self.clients if isinstance(c, Interface)]
        now = _time.monotonic()
        topic_status = []
        for t in sorted(topics):
            itd = self.topic_content.get(t)
            if itd is None:
                topic_status.append(TopicStatus(topic=t))
                continue
            ts_val = itd.data.meta.get("ts")
            stub = itd.data.stub
            size = None if stub else len(itd.data.value.value.encode())
            stored_ago = round(now - itd.stored_at, 3)
            topic_status.append(TopicStatus(
                topic=t,
                ts=str(ts_val) if ts_val else None,
                stub=stub,
                size=size,
                stored_ago=stored_ago,
            ))
        return StatusResult(
            ok=True,
            topics=list(sorted(topics)),
            topic_status=topic_status,
            clients=clients,
            xsel=get_xsel_status(self.app),
            proxy=get_proxy_status(self.app),
        )

    def _emit_runtime_state_change(self, reason: str) -> None:
        hooks = list(getattr(self.app.state, "runtime_state_hooks", []))
        for hook in hooks:
            task = asyncio.create_task(
                hook(self.app, reason),
                name=f"runtime_state_{reason}",
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def dispatcher(self):
        while True:
            try:
                await self.process_queue()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                error(f"dispatcher error: {e}", exc_info=True)

    def _accept_incoming(self, incoming: InternalTopicData,
                         existing: InternalTopicData | None) -> bool:
        """Conflict resolution: decide whether `incoming` replaces `existing`.

        Strategy ("newer wins; tie → local"):
        - No existing value, or either side lacks a comparable timestamp →
          accept (backwards compatible: legacy items without meta["ts"] behave
          as today).
        - incoming is newer than existing by more than the tie window →
          accept (newer wins).
        - incoming is older than existing by more than the tie window →
          drop (older loses).
        - Within the tie window (treated as a tie) → keep existing, *unless*
          the existing value came from a remote peer (proxy) and the incoming
          one is local, in which case the local value wins the tie.

        Timestamps on the incoming item are already normalised to this host's
        clock (see ProxyClient clock-offset exchange). The tie window absorbs
        the residual error of that estimate (~RTT/2) and minor clock skew, so
        near-simultaneous values resolve deterministically to the local one.
        """
        if existing is None:
            return True
        new_ts = incoming.compare_ts
        old_ts = existing.compare_ts
        if new_ts is None or old_ts is None:
            return True
        delta = new_ts - old_ts
        if delta > self.sync_tie_window:
            return True
        if delta < -self.sync_tie_window:
            return False
        # Tie (within tolerance): keep existing unless local supersedes remote.
        return existing.is_remote and not incoming.is_remote

    async def _process_data_item(self, topic_data: InternalTopicData):
        assert isinstance(topic_data, InternalTopicData)
        existing = self.topic_content.get(topic_data.topic)
        if not self._accept_incoming(topic_data, existing):
            debug(
                "conflict: dropping older/tie item topic=%s new_ts=%s old_ts=%s "
                "new_remote=%s old_remote=%s",
                topic_data.topic,
                topic_data.compare_ts,
                existing.compare_ts if existing else None,
                topic_data.is_remote,
                existing.is_remote if existing else None,
            )
            topic_data.rejected = True
            return
        self.topic_content[topic_data.topic] = topic_data

    def _dispatch_data_item(self, topic_data: InternalTopicData) -> None:
        subs: set[Interface] | None = self.subs.get(topic_data.topic)
        if not subs:
            return
        source = topic_data.source
        encrypted = topic_data.data.meta.get("encrypted") is True
        val_len = len(topic_data.data.value.value.encode())
        now = _time.monotonic()
        tm = self.topic_meta.get(topic_data.topic)
        for conn in subs:
            if isinstance(conn, BidirectionalInterface):
                if source and conn is source:
                    continue
                if encrypted:
                    pub_key = getattr(conn, "public_key", None)
                    if not pub_key or pub_key not in self.public_keys:
                        continue
                data = topic_data.data
                if (self.lazy_local_threshold > 0
                        and val_len >= self.lazy_local_threshold
                        and getattr(conn, "_is_rpc_transport", False)):
                    data = data.model_copy(update={
                        "value": ValueData(value="", type="text", encoding="plain"),
                        "stub": True,
                        "fetch_url": f"/v1/clip/{data.topic}",
                    })
                conn.deliver(data)
                # monitoring counters
                ci = self.client_info.get(conn)
                if ci:
                    ci.notify_count += 1
                    ci.last_notify_at = now
                if tm:
                    tm.notify_count += 1
                    tm.notified_clients[ci.conn_id if ci else repr(conn)] = now
                self._emit_monitor(MonitorEvent(
                    kind=MonitorEventKind.TOPIC_NOTIFY,
                    ts=now,
                    ts_utc=self._utcnow(),
                    conn_id=ci.conn_id if ci else None,
                    topic=topic_data.topic,
                    data={},
                ))

    async def _notify_topic_data(self, topic_data: InternalTopicData) -> None:
        self._dispatch_data_item(topic_data)

    async def _delayed_notify(self, topic: str) -> None:
        try:
            await asyncio.sleep(self.notify_delay_ms / 1000.0)
            await self.flush_topic_notification(topic)
        except asyncio.CancelledError:
            raise
        finally:
            async with self.notification_lock:
                current = self.notification_tasks.get(topic)
                if current is asyncio.current_task():
                    self.notification_tasks.pop(topic, None)

    async def _schedule_notification(self,
                                     topic_data: InternalTopicData) -> None:
        if self.notify_delay_ms <= 0:
            await self._notify_topic_data(topic_data)
            return
        async with self.notification_lock:
            self.pending_notifications[topic_data.topic] = topic_data
            task = self.notification_tasks.get(topic_data.topic)
            if task is None or task.done():
                self.notification_tasks[topic_data.topic] = (
                    asyncio.create_task(
                        self._delayed_notify(topic_data.topic),
                        name=f"notify_{topic_data.topic}",
                    ))

    async def flush_topic_notification(self, topic: str) -> None:
        async with self.notification_lock:
            topic_data = self.pending_notifications.pop(topic, None)
        if topic_data is None:
            return
        await self._notify_topic_data(topic_data)

    async def cancel_background_tasks(self) -> None:
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def flush_all_notifications(self) -> None:
        async with self.notification_lock:
            pending = dict(self.pending_notifications)
            tasks = list(self.notification_tasks.values())
            self.pending_notifications.clear()
            self.notification_tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for topic_data in pending.values():
            await self._notify_topic_data(topic_data)

    async def process_put_item(self, topic_data: InternalTopicData):
        await self._process_data_item(topic_data)
        if topic_data.rejected:
            # Conflict resolution dropped this item (older / lost a tie): it was
            # not stored, so do not update monitoring state or notify anyone.
            return
        # monitoring: update TopicMeta and ClientInfo
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
            stored_at_utc=topic_data.stored_at_utc or self._utcnow(),
            source_id=eff_conn_id,
            source_app=eff_app,
            source_addr=ci.addr if ci else None,
        )
        self._emit_monitor(MonitorEvent(
            kind=MonitorEventKind.TOPIC_PUT,
            ts=now,
            ts_utc=self._utcnow(),
            conn_id=eff_conn_id,
            topic=topic_data.topic,
            data={"size": size, "app": eff_app},
        ))
        await self._schedule_notification(topic_data)

    async def process_get_item(self, subject: str, topic: str,
                               requester: "Interface | None" = None) -> ItemType:
        debug(f"process_get_item subject='{subject}' topic='{topic}'")
        content = None
        if subject == "topic":
            assert topic
            content = self.topic_content.get(topic)
            debug(
                f"found content for topic '{topic}': '{content}' from: '{self.topic_content}'"
            )
            # monitoring counters for clip.get
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
                self._emit_monitor(MonitorEvent(
                    kind=MonitorEventKind.TOPIC_GET,
                    ts=now,
                    ts_utc=self._utcnow(),
                    conn_id=ci.conn_id if ci else None,
                    topic=topic,
                    data={},
                ))
        elif subject == "topics":
            content = list(self.topic_content.keys())
        else:
            content = None

        return content

    async def process_queue(self):
        debug("bus.get -> item")
        item: GenericSerialCall = await self.bus.get()
        debug(f"calling item: {item}")
        await item.call(self)
        self.bus.task_done()
        debug("process_queue: done")

    async def enqueue_request(
        self,
        action: str,
        data: Any,
        requester: "Interface | None" = None,
    ) -> TopicData | list[str] | None:
        if action.startswith("get:"):
            if action.endswith(":topic"):
                assert isinstance(data, str)
                await self.flush_topic_notification(data)
                req = GetTopicData(data, None, requester=requester)
                debug(f"put request: {req}")
                await self.bus.put(req)
                debug("wait for future")
                await req.future
                internal_topic_data: InternalTopicData | None = (
                    req.future.result())
                if internal_topic_data:
                    return internal_topic_data.data
            if action.endswith(":topics"):
                topics = list(self.topic_content.keys())
                return topics
        else:
            raise ValueError("Bad request")
        return None

    async def enqueue_topic_data(self, data: InternalTopicData) -> None:
        req = SetTopicData(data, None)
        await self.bus.put(req)
        await req.future

    async def enqueue_topic_data_nowait(self, data: InternalTopicData):
        await self.bus.put_nowait(SetTopicData(data, None))


def subscribe_client(app: FastAPI, client: Interface,
                     topics: list[str]) -> dict[str, TopicData]:
    assert isinstance(app.state.main, AppState)
    main: AppState = app.state.main
    return main.subscribe_client(client, topics)


def register_client(app: FastAPI, client: Interface):
    assert isinstance(app.state.main, AppState)
    main: AppState = app.state.main
    main.register_client(client)


def unregister_client(app: FastAPI, client: Interface):
    assert isinstance(app.state.main, AppState)
    main: AppState = app.state.main
    main.unregister_client(client)


def unsubscribe_client(app: FastAPI, client: Interface, topics: list[str]):
    assert isinstance(app.state.main, AppState)
    main: AppState = app.state.main
    main.unsubscribe_client(client, topics)


async def enqueue_request_topic(
    app: FastAPI,
    topic: str,
    requester: Interface | None = None,
) -> TopicData | None:
    assert isinstance(app.state.main, AppState)
    main: AppState = app.state.main
    result = await main.enqueue_request("get:topic", topic, requester=requester)
    if result and isinstance(result, TopicData):
        return result
    return None


async def enqueue_request_topics(app: FastAPI) -> list[str] | None:
    assert isinstance(app.state.main, AppState)
    main: AppState = app.state.main
    result = await main.enqueue_request("get:topics", None)
    if result:
        assert isinstance(result, list)
    return result


def _make_client_info(client: Interface) -> ClientInfo:
    from rclipboard.proxy import ProxyClient
    from rclipboard.xsel import XselInterface
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


def _resolve_compare_ts(data: TopicData,
                        compare_ts: "datetime.datetime | None"):
    """Pick the timestamp used for conflict resolution.

    An explicit ``compare_ts`` (already normalised to this host's clock by a
    peer with a known offset) wins. Otherwise fall back to parsing the item's
    own ``meta["ts"]`` — for locally-originated items that wall clock *is* this
    host's clock, so it is directly comparable.
    """
    from rclipboard.helpers import parse_utc_timestamp
    if compare_ts is not None:
        return compare_ts
    return parse_utc_timestamp(data.meta.get("ts"))


async def enqueue_topic_data(app: FastAPI, data: TopicData,
                             source: Interface | None,
                             monitor_conn_id: str | None = None,
                             monitor_app: str | None = None,
                             compare_ts: "datetime.datetime | None" = None
                             ) -> InternalTopicData:
    internal_topic_data = InternalTopicData(
        data=data, source=source,
        monitor_conn_id=monitor_conn_id, monitor_app=monitor_app,
        compare_ts=_resolve_compare_ts(data, compare_ts))
    assert isinstance(app.state.main, AppState)
    main: AppState = app.state.main
    await main.enqueue_topic_data(internal_topic_data)
    return internal_topic_data


async def enqueue_topic_data_nowait(app: FastAPI, data: TopicData,
                                    source: Interface | None,
                                    monitor_conn_id: str | None = None,
                                    monitor_app: str | None = None,
                                    compare_ts: "datetime.datetime | None" = None):
    internal_topic_data = InternalTopicData(
        data=data, source=source,
        monitor_conn_id=monitor_conn_id, monitor_app=monitor_app,
        compare_ts=_resolve_compare_ts(data, compare_ts))
    await app.state.main.enqueue_topic_data_nowait(internal_topic_data)
    return internal_topic_data
