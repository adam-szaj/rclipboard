import asyncio
from abc import ABC, abstractmethod
from logging import Logger
from typing import Any, Callable, Generic, TypeVar, override

from fastapi import FastAPI

from app.log import get_logger
from app.types import Connection, InternalTopicData, TopicData

logger: Logger = get_logger(__name__)
error = logger.error
warning = logger.warning
info = logger.info
debug = logger.debug
# trace = logger.trace
# backtrace = logger.backtrace

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
    def __init__(self, topic_data: InternalTopicData,
                 future: asyncio.Future[None] | None):
        super().__init__(future)
        self.topic_data: InternalTopicData = topic_data

    @override
    async def do_call(self, app: "AppState") -> None:
        info(f"put item: '{self.topic_data}'")
        await app.proces_put_item(self.topic_data)


class GetTopicData(SerialCall[InternalTopicData]):
    def __init__(self, topic: str,
                 future: asyncio.Future[InternalTopicData] | None):
        super().__init__(future)
        self.topic: str = topic

    @override
    async def do_call(self, app: "AppState") -> InternalTopicData:
        item: ItemType = await app.proces_get_item("topic", self.topic)
        assert isinstance(item, InternalTopicData)
        return item


GenericSerialCall = GetTopicData | SetTopicData


class AppState:
    def __init__(self, app: FastAPI):
        self.bus: Bus[GenericSerialCall] = Bus[GenericSerialCall]()
        self.clients: list[Connection] = []
        self.topic_content: dict[str, InternalTopicData] = {}
        self.subs: dict[str, set[Connection]] = {}
        self.dispatcher_task: asyncio.Task[Callable[[], None]] = (
                asyncio.create_task(self.dispatcher(), name="dispatcher")
                )

    def subsctibe_client(self, client: Connection, topics: list[str]):
        for topic in topics:
            if topic not in self.subs:
                self.subs[topic] = set()
            self.subs[topic].add(client)

    def register_client(self, client: Connection):
        self.clients.append(client)

    def unregister_client(self, client: Connection):
        self.clients.remove(client)

    async def dispatcher(self):
        while True:
            try:
                await self.process_queue()
            except RuntimeError as e:
                error(f"Error: {e}")

    async def _proces_data_item(self, topic_data: InternalTopicData):
        assert isinstance(topic_data, InternalTopicData)
        self.topic_content[topic_data.topic] = topic_data

    async def _dispatch_data_item(self, topic_data: InternalTopicData):
        subs: set[Connection] | None = self.subs.get(topic_data.topic)
        info(f"subs: {subs}")
        if not subs:
            return
        info(f"dispatch topic_data: {topic_data}")
        source = topic_data.source
        for conn in subs:
            if source and str(conn) == source:
                continue
            await conn.send(topic_data)

    async def proces_put_item(self, topic_data: InternalTopicData):
        # Update content store and fan-out
        await self._proces_data_item(topic_data)
        await self._dispatch_data_item(topic_data)

    async def proces_get_item(self, subject: str, topic: str) -> ItemType:
        info(f"subject: '{subject}' topic: '{topic}'")
        content = None
        if subject == "topic":
            assert topic
            content = self.topic_content.get(topic)
            info(
                f"found content for topic '{topic}': '{content}' from: '{
                    self.topic_content
                }'"
            )
        elif subject == "topics":
            content = list(self.topic_content.keys())
        else:
            content = None

        return content

    async def process_queue(self):
        info("wait for item")
        try:
            item: GenericSerialCall = await self.bus.get()
            info(f"got item: {item}")
            self.bus.task_done()
            await item.call(self)
        except AssertionError as e:
            tb = e.__traceback__
            while tb:
                error(f"{tb.tb_frame}:{tb.tb_lineno}")
                tb = tb.tb_next
            error(f"AssertionError: {e.args}")
            raise
        except Exception as e:
            tb = e.__traceback__
            while tb:
                error(f"{tb.tb_frame}:{tb.tb_lineno}")
                tb = tb.tb_next
            error(f"Exception '{type(e)}': '{e}'")
            raise

        print("process_queue: done.. ")

    async def enqueue_request(self, action: str, data: Any) -> TopicData | None:
        print(f"action: {action} data: {data}")
        if action.startswith("get:"):
            if action.endswith(":topic"):
                assert isinstance(data, str)
                req = GetTopicData(data, None)
                debug(f"put request: {req}")
                await self.bus.put(req)
                debug("wait for future")
                await req.future
                internal_topic_data: InternalTopicData = req.future.result()
                if internal_topic_data:
                    return internal_topic_data.data
        else:
            raise ValueError("Bad request")
        return None

    async def enqueue_topic_data(self, data: InternalTopicData) -> None:
        info(f"data: {data}")
        req = SetTopicData(data, None)
        await self.bus.put(req)
        await req.future

    async def enqueue_topic_data_nowait(self, data: InternalTopicData):
        info(f"data: {data}")
        await self.bus.put_nowait(self.SetTopicData(data))


def subsctibe_client(app: FastAPI, client: Connection, topics: list[str]):
    assert isinstance(app.state.main, AppState)
    main: AppState = app.state.main
    main.subsctibe_client(client, topics)


def register_client(app: FastAPI, client: Connection):
    assert isinstance(app.state.main, AppState)
    main: AppState = app.state.main
    main.register_client(client)


def unregister_client(app: FastAPI, client: Connection):
    assert isinstance(app.state.main, AppState)
    main: AppState = app.state.main
    main.unregister_client(client)


async def enqueue_request_topic(app: FastAPI, topic: str) -> TopicData | None:
    assert isinstance(app.state.main, AppState)
    main: AppState = app.state.main
    return await main.enqueue_request("get:topic", topic)


async def enqueue_request_topics(app: FastAPI) -> TopicData | None:
    assert isinstance(app.state.main, AppState)
    main: AppState = app.state.main
    return await main.enqueue_request("get:topics", None)


async def enqueue_topic_data(app: FastAPI, data: TopicData,
                             source: Connection | None) -> InternalTopicData:
    internal_topic_data = InternalTopicData(data=data, source=source)
    assert isinstance(app.state.main, AppState)
    main: AppState = app.state.main
    await main.enqueue_topic_data(internal_topic_data)
    return internal_topic_data


async def enqueue_topic_data_nowait(
    app: FastAPI, data: TopicData, source: Connection | None
):
    internal_topic_data = InternalTopicData(data=data, source=source)
    await app.state.main.enqueue_topic_data_nowait(internal_topic_data)
    return internal_topic_data
