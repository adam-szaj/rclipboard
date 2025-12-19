import asyncio
from abc import ABC, abstractmethod
from logging import Logger
from typing import Any, Generic, TypeVar, override

from fastapi import FastAPI
from app.log import get_logger
from .types import Connection, InternalTopicData, TopicData

logger: Logger = get_logger(__name__)
error = logger.error
warning = logger.warning
info = logger.info
debug = logger.debug
trace = logger.trace
backtrace = logger.backtrace

T = TypeVar("T")


class Bus(Generic[T]):
    def __init__(self, **kwargs):
        self.q: asyncio.Queue[T] = asyncio.Queue[T](**kwargs)

    def task_done(self):
        self.q.task_done()

    async def get_nowait(self):
        return self.q.get_nowait()

    async def get(self):
        return await self.q.get()

    async def put(self, data: InternalTopicData):
        return await self.q.put(data)

    async def request(self, req):
        pass

    async def put_nowait(self, data: InternalTopicData):
        return self.q.put_nowait(data)


def make_dict(**kwargs) -> dict[str, object]:
    """Create a generic message dict from keyword arguments."""
    return kwargs


def make_topic_data(
    *, source: Connection, topic: str, value: str, type: str, encoding: str, app: str
):
    return make_dict(
        source=source,
        topic=topic,
        value=make_dict(value=value, type=type, encoding=encoding),
        meta=make_dict(app=app),
    )


class AppState:
    def __init__(self, app: FastAPI):
        self.bus: Bus = Bus[Any]()
        self.clients: list[Connection] = []
        self.topic_content: dict[str, object] = {}
        self.subs: dict[str, set[Connection]] = {}
        self.dispatcher_task = asyncio.create_task(self.dispatcher(), name="dispatcher")

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
                error(f"fail: {e}")

    class SerialCall(ABC):
        def __init__(self):
            self._future = asyncio.Future()

        def future(self) -> asyncio.Future:
            return self._future

        @abstractmethod
        async def do_call(self, app: "AppState"):
            pass

        async def call(self, app: "AppState"):
            try:
                result = await self.do_call(app)
                self._future.set_result(result)
            except Exception as e:
                self._future.set_exception(e)

    class SetTopicData(SerialCall):
        def __init__(self, topic_data: InternalTopicData):
            super().__init__()
            self.topic_data = topic_data

        @override
        async def do_call(self, app: "AppState"):
            info(f"put item: '{self.topic_data}'")
            await app._proces_put_item(self.topic_data)

    class GetTopicData(SerialCall):
        def __init__(self, topic: str):
            super().__init__()
            self.topic: str = topic

        @override
        async def do_call(self, app: "AppState"):
            return await app._proces_get_item("topic", self.topic)

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
            await conn.enqueue_topic_data(topic_data)

    async def _proces_put_item(self, topic_data: InternalTopicData):
        # Update content store and fan-out
        await self._proces_data_item(topic_data)
        await self._dispatch_data_item(topic_data)

    async def _proces_get_item(self, subject: str, topic: str):
        info(f"subject: '{subject}' topic: '{topic}'")
        if subject == "topic":
            assert topic
            content = self.topic_content.get(topic, "")
            info(
                f"found content for topic '{topic}': '{content}' from: '{
                    self.topic_content
                }'"
            )
        elif subject == "topics":
            content = self.topic_content.keys()
        else:
            content = {"message": f"unsupported request subject: '{subject}'"}
        return content

    async def process_queue(self):
        info("wait for item")
        try:
            item: SerialCall = await self.bus.get()
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

    async def enqueue_request(self, action: str, data: T) -> TopicData | None:
        print(f"action: {action} data: {data}")

        loop = self.dispatcher_task.get_loop()
        if action.startswith("get:"):
            topic = action.split(":")[1]
            req = self.GetTopicData(data)

            trace(f"put request: {req}")
            await self.bus.put(req)
            trace("wait for future")

            await req.future()
            internal_topic_data: InternalTopicData = req.future().result()
            if internal_topic_data:
                return internal_topic_data.data
        else:
            raise ValueError("Bad request")
        return None


    async def enqueue_topic_data(self, data: InternalTopicData):
        info(f"data: {data}")
        req = self.SetTopicData(data)
        await self.bus.put(req)
        await req.future()

    async def enqueue_topic_data_nowait(self, data: InternalTopicData):
        info(f"data: {data}")
        await self.bus.put_nowait(self.SetTopicData(data))


def subsctibe_client(app: FastAPI, client: Connection, topics: list[str]):
    app.state.main.subsctibe_client(client, topics)


def register_client(app: FastAPI, client: Connection):
    app.state.main.register_client(client)


def unregister_client(app: FastAPI, client: Connection):
    app.state.main.unregister_client(client)


async def enqueue_request_topics(app: FastAPI):
    return await app.state.main.enqueue_request("get:topics")


async def enqueue_request_topic(app: FastAPI, topic: str) -> TopicData | None:
    return await app.state.main.enqueue_request("get:topic", topic)


async def enqueue_topic_request(app: FastAPI, topic: str) -> TopicData | None:
    return await enqueue_request_topic(app, topic)


async def enqueue_topic_data(app: FastAPI, data: TopicData, source: Connection | None) :
    info(f"data: {data}")
    internal_topic_data = InternalTopicData(data=data, source=source)
    await app.state.main.enqueue_topic_data(internal_topic_data)
    return internal_topic_data


async def enqueue_topic_data_nowait(app: FastAPI, data: TopicData, source: Connection | None):
    internal_topic_data = InternalTopicData(data=data, source=source)
    await app.state.main.enqueue_topic_data_nowait(internal_topic_data)
    return internal_topic_data
