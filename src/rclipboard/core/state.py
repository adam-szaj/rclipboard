"""Central application state: topic storage, subscriber dispatch, debounced
notifications, conflict resolution and the app.state.main facade."""
import asyncio
import contextlib
import datetime
import os
import time as _time
from logging import Logger

from fastapi import FastAPI

from rclipboard.core.bus import (
    GenericSerialCall,
    GetTopicData,
    GetTopics,
    SetTopicData,
)
from rclipboard.core.interfaces import BidirectionalInterface, Interface
from rclipboard.core.monitoring import MonitorHub
from rclipboard.core.topics import InternalTopicData
from rclipboard.log import get_logger
from rclipboard.models.convert import stub_topic_data
from rclipboard.models.wire import (
    HealthResult,
    StatusResult,
    TopicData,
    TopicStatus,
)
from rclipboard.timeutil import parse_utc_timestamp, utc_timestamp

logger: Logger = get_logger(__name__)
error = logger.error
warning = logger.warning
info = logger.info
debug = logger.debug


class AppState:

    def __init__(self, app: FastAPI):
        self.app: FastAPI = app
        self.bus: asyncio.Queue[GenericSerialCall] = asyncio.Queue()
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
        # Retention for encrypted items: they expire this many seconds after
        # their write time (meta["ts"]); on expiry the value is blanked to "".
        # 0 disables the feature (encrypted items never expire). Default 30 s.
        self.encrypted_ttl: datetime.timedelta = datetime.timedelta(
            seconds=int(os.environ.get("RCLIPBOARD_ENCRYPTED_TTL_S", "30")))
        self.pending_notifications: dict[str, InternalTopicData] = {}
        self.notification_tasks: dict[str, asyncio.Task[None]] = {}
        self.notification_lock = asyncio.Lock()
        self.public_keys: dict[str, dict] = {}  # age1pubkey → {public_key, label, key_id}
        self._background_tasks: set[asyncio.Task] = set()
        # monitoring — all telemetry state and event emission live in the hub
        self.monitor: MonitorHub = MonitorHub()
        self.dispatcher_task: asyncio.Task[None] = asyncio.create_task(
            self.dispatcher(), name="dispatcher")

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
        # Lazy import: rpc_handler imports core.state, so importing at module
        # scope would be circular.
        from rclipboard.transports.rpc_handler import RPCHandler
        ts = self._utcnow()
        params = {"reason": reason, "ts_utc": ts}
        for client in list(self.clients):  # snapshot — sending may mutate clients
            if isinstance(client, RPCHandler):
                try:
                    await client._send_event("server.shutdown", params)
                except Exception:
                    pass  # dead/closed connection — ignore during shutdown
        self.monitor.service_stop(reason, ts)
        # Let monitor handlers consume + flush their queues (and self-close) before
        # the caller proceeds to uvicorn's connection teardown. Bounded so a stuck
        # consumer can't delay shutdown.
        await self.monitor.drain(1.0)

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
                self._expire_if_needed(content)
                contents[topic] = content.data
        self.monitor.client_subscribed(client, topics)
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
        self.monitor.client_unsubscribed(client, topics)
        self._emit_runtime_state_change("subscriptions")

    def register_client(self, client: Interface):
        self.clients.append(client)
        if isinstance(client, BidirectionalInterface):
            client.start_drainer()
        self.monitor.client_connected(client)
        self._emit_runtime_state_change("clients")

    def unregister_client(self, client: Interface):
        self.clients.remove(client)
        self.monitor.client_disconnected(client)
        self._emit_runtime_state_change("clients")

    def get_health(self) -> HealthResult:
        from rclipboard.transports.pasteboard import get_pasteboard_status
        from rclipboard.transports.proxy import get_proxy_status
        from rclipboard.transports.xsel import get_xsel_status
        xsel = get_xsel_status(self.app)
        pasteboard = get_pasteboard_status(self.app)
        proxy = get_proxy_status(self.app)
        return HealthResult(
            ok=True,
            xsel_enabled=bool(xsel["enabled"]),
            xsel_good=bool(xsel["good"]),
            pasteboard_enabled=bool(pasteboard["enabled"]),
            pasteboard_good=bool(pasteboard["good"]),
            proxy_enabled=bool(proxy["enabled"]),
            proxy_good=bool(proxy["good"]),
        )

    def get_status(self, topics: list[str]) -> StatusResult:
        from rclipboard.transports.pasteboard import get_pasteboard_status
        from rclipboard.transports.proxy import get_proxy_status
        from rclipboard.transports.xsel import get_xsel_status
        clients = [c.name for c in self.clients if isinstance(c, Interface)]
        now = _time.monotonic()
        topic_status = []
        for t in sorted(topics):
            itd = self.topic_content.get(t)
            if itd is None:
                topic_status.append(TopicStatus(topic=t))
                continue
            self._expire_if_needed(itd)
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
            pasteboard=get_pasteboard_status(self.app),
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

        Strategy ("newer wins; tie → local"), applied only ACROSS hosts:
        - Messages originating on one host (see InternalTopicData.origin_host)
          count in arrival order — timestamps only arbitrate between
          *different* hosts. Clocks on one host are the same clock, so a rapid
          local succession must never be dropped as a "tie".
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
        if incoming.origin_host == existing.origin_host:
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
        for conn in subs:
            if isinstance(conn, BidirectionalInterface):
                if source and conn is source:
                    continue
                if encrypted:
                    # Encrypted values go only to connections that presented
                    # at least one registered key (a proxy presents the keys
                    # of all its downstream clients). Duck-typed to avoid a
                    # core -> transports import.
                    presented = getattr(conn, "presented_keys", None)
                    if presented is None:
                        pk = getattr(conn, "public_key", None)
                        presented = {pk} if pk else set()
                    if not any(k in self.public_keys for k in presented):
                        continue
                data = topic_data.data
                if (self.lazy_local_threshold > 0
                        and val_len >= self.lazy_local_threshold
                        and getattr(conn, "_is_rpc_transport", False)):
                    data = stub_topic_data(data)
                conn.deliver(data)
                self.monitor.topic_notified(conn, topic_data.topic, now)

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
        self.monitor.record_put(topic_data)
        await self._schedule_notification(topic_data)

    def _expire_if_needed(self, itd: InternalTopicData) -> bool:
        """Blank an encrypted item's value once its retention window elapses.

        Encrypted items expire ``encrypted_ttl`` seconds after their write
        time; on expiry the stored value is replaced by the empty string (the
        topic keeps existing, the ``encrypted`` flag is preserved, and
        ``meta["expired"]`` is set). Mutates ``itd`` in place so the ciphertext
        is dropped from RAM on the first read that touches it.

        Returns True only on the transition (freshly expired this call), so
        callers can act on it; a no-op (disabled, plain item, no comparable
        timestamp, still fresh, or already blanked) returns False.
        """
        if self.encrypted_ttl <= datetime.timedelta(0):
            return False  # feature disabled
        if itd.data.meta.get("encrypted") is not True:
            return False
        if not itd.data.value.value:
            return False  # already blanked
        # Prefer the clock-normalised compare_ts (remote items), else the
        # item's own meta["ts"] (locally-written items share our clock).
        ref = itd.compare_ts or parse_utc_timestamp(itd.data.meta.get("ts"))
        if ref is None:
            return False  # no comparable timestamp — cannot expire (legacy)
        now = parse_utc_timestamp(utc_timestamp())
        assert now is not None
        if now - ref <= self.encrypted_ttl:
            return False  # still within retention
        itd.data.value.value = ""
        itd.data.meta["expired"] = True
        debug("encrypted item expired: topic=%s age=%.1fs ttl=%.0fs",
              itd.topic, (now - ref).total_seconds(),
              self.encrypted_ttl.total_seconds())
        return True

    def get_topic_item(
            self, topic: str,
            requester: "Interface | None" = None
    ) -> InternalTopicData | None:
        """Read one topic's stored item and record the get in monitoring.

        Runs on the dispatcher (via GetTopicData), so it sees the store in a
        serialised state with respect to concurrent puts. Expired encrypted
        items are blanked before returning.
        """
        content = self.topic_content.get(topic)
        if content is not None:
            self._expire_if_needed(content)
        self.monitor.record_get(topic, content, requester)
        return content

    def get_topic_list(
            self, requester: "Interface | None" = None) -> list[str]:
        """List all stored topics; runs on the dispatcher (via GetTopics)."""
        _ = requester
        return list(self.topic_content.keys())

    async def process_queue(self):
        item: GenericSerialCall = await self.bus.get()
        await item.call(self)
        self.bus.task_done()

    async def request_topic(
        self,
        topic: str,
        requester: "Interface | None" = None,
    ) -> TopicData | None:
        """Fetch one topic's value. Flushes that topic's pending notification
        first so the returned value reflects the latest buffered write."""
        await self.flush_topic_notification(topic)
        req = GetTopicData(topic, requester=requester)
        await self.bus.put(req)
        item = await req.future
        return item.data if item else None

    async def request_topics(
        self,
        requester: "Interface | None" = None,
    ) -> list[str]:
        """List all topics. Flushes ALL pending notifications first so the
        returned set reflects every buffered write."""
        await self.flush_all_notifications()
        req = GetTopics(requester=requester)
        await self.bus.put(req)
        return await req.future

    async def enqueue_topic_data(self, data: InternalTopicData) -> None:
        req = SetTopicData(data)
        await self.bus.put(req)
        await req.future

    def enqueue_topic_data_nowait(self, data: InternalTopicData):
        self.bus.put_nowait(SetTopicData(data))


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
    return await main.request_topic(topic, requester=requester)


async def enqueue_request_topics(app: FastAPI) -> list[str] | None:
    assert isinstance(app.state.main, AppState)
    main: AppState = app.state.main
    return await main.request_topics()


def _resolve_compare_ts(data: TopicData,
                        compare_ts: "datetime.datetime | None"):
    """Pick the timestamp used for conflict resolution.

    An explicit ``compare_ts`` (already normalised to this host's clock by a
    peer with a known offset) wins. Otherwise fall back to parsing the item's
    own ``meta["ts"]`` — for locally-originated items that wall clock *is* this
    host's clock, so it is directly comparable.
    """
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
    app.state.main.enqueue_topic_data_nowait(internal_topic_data)
    return internal_topic_data
