import asyncio
import contextlib
import os
from abc import ABC, abstractmethod
from logging import Logger
from typing import Any, Generic, TypeVar, override

from fastapi import FastAPI

from rclipboard.log import get_logger
from rclipboard.types import (
    BidirectionalInterface,
    Interface,
    InternalTopicData,
    TopicData,
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
    ):
        super().__init__(future)
        self.topic: str = topic

    @override
    async def do_call(self, app: "AppState") -> InternalTopicData | None:
        item: ItemType = await app.process_get_item("topic", self.topic)
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
        self.pending_notifications: dict[str, InternalTopicData] = {}
        self.notification_tasks: dict[str, asyncio.Task[None]] = {}
        self.notification_lock = asyncio.Lock()
        self._background_tasks: set[asyncio.Task] = set()
        self.dispatcher_task: asyncio.Task[None] = asyncio.create_task(
            self.dispatcher(), name="dispatcher")

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
        self._emit_runtime_state_change("subscriptions")

    def register_client(self, client: Interface):
        self.clients.append(client)
        if isinstance(client, BidirectionalInterface):
            client.start_drainer()
        self._emit_runtime_state_change("clients")

    def unregister_client(self, client: Interface):
        self.clients.remove(client)
        self._emit_runtime_state_change("clients")

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

    async def _process_data_item(self, topic_data: InternalTopicData):
        assert isinstance(topic_data, InternalTopicData)
        self.topic_content[topic_data.topic] = topic_data

    def _dispatch_data_item(self, topic_data: InternalTopicData) -> None:
        subs: set[Interface] | None = self.subs.get(topic_data.topic)
        if not subs:
            return
        source = topic_data.source
        for conn in subs:
            if isinstance(conn, BidirectionalInterface):
                if source and conn is source:
                    continue
                conn.deliver(topic_data.data)

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
        # Update content store and fan-out
        await self._process_data_item(topic_data)
        await self._schedule_notification(topic_data)

    async def process_get_item(self, subject: str, topic: str) -> ItemType:
        info(f"subject: '{subject}' topic: '{topic}'")
        content = None
        if subject == "topic":
            assert topic
            content = self.topic_content.get(topic)
            debug(
                f"found content for topic '{topic}': '{content}' from: '{self.topic_content}'"
            )
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

    async def enqueue_request(self, action: str,
                              data: Any) -> TopicData | list[str] | None:
        # warning(f"action: {action} data: {data}")
        # if not data:
        #     traceback.print_stack()
        if action.startswith("get:"):
            if action.endswith(":topic"):
                assert isinstance(data, str)
                await self.flush_topic_notification(data)
                req = GetTopicData(data, None)
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


async def enqueue_request_topic(app: FastAPI, topic: str) -> TopicData | None:
    assert isinstance(app.state.main, AppState)
    main: AppState = app.state.main
    result = await main.enqueue_request("get:topic", topic)
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


async def enqueue_topic_data(app: FastAPI, data: TopicData,
                             source: Interface | None) -> InternalTopicData:
    internal_topic_data = InternalTopicData(data=data, source=source)
    assert isinstance(app.state.main, AppState)
    main: AppState = app.state.main
    await main.enqueue_topic_data(internal_topic_data)
    return internal_topic_data


async def enqueue_topic_data_nowait(app: FastAPI, data: TopicData,
                                    source: Interface | None):
    internal_topic_data = InternalTopicData(data=data, source=source)
    await app.state.main.enqueue_topic_data_nowait(internal_topic_data)
    return internal_topic_data
