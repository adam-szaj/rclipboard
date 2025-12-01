from json import JSONEncoder
import json
from fastapi import FastAPI
from typing import Any
from abc import ABC, abstractmethod
import asyncio

from logging import Logger
from app.log import get_logger
from messages import utc_timestamp

logger: Logger = get_logger(__name__)
error = logger.error
warning = logger.warning
info = logger.info
debug = logger.debug
trace = logger.debug


class Bus:

    def __init__(self, **kwargs):
        self.q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(**kwargs)

    def task_done(self):
        self.q.task_done()

    async def get_nowait(self):
        return await self.q.get_nowait()

    async def get(self):
        return await self.q.get()

    async def put(self, data):
        return await self.q.put(data)

    async def put_nowait(self, data):
        return await self.q.put_nowait(data)


class Connection(ABC):

    def __init__(self):
        pass

    @abstractmethod
    async def enqueue_topic_data(self, data: dict[str, object]):
        pass

    @abstractmethod
    async def send(self, data: dict[str, object]):
        pass

    def __repr__(self) -> str:
        return "conn"

    def __str__(self) -> str:
        return "conn"


class ConnectionEncoder(JSONEncoder):

    def default(self, o):
        if isinstance(o, Connection):
            return {"name": str(o)}
        else:
            return JSONEncoder.default(o)


def make_dict(**kwargs) -> dict[str, object]:
    """Create a generic message dict from keyword arguments."""
    return kwargs


def make_topic_data(*, source: Connection, topic: str, value: str, type: str,
                    encoding: str, app: str):
    return make_dict(source=source,
                     data=make_dict(topic=topic,
                                    value=value,
                                    type=type,
                                    encoding=encoding),
                     meta=make_dict(app=app))


class AppState:

    def __init__(self, app: FastAPI):
        self.bus: Bus = Bus()
        self.clients: list[Connection] = []
        self.topic_content: dict[str, object] = {}
        self.subs: dict[str, set[Connection]] = {}
        self.dispatcher_task = asyncio.create_task(self.dispatcher(),
                                                   name="dispatcher")

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

    async def _proces_data_item(self, item: dict[str, object]):
        data0 = item.get("data", {})
        data = data0.get("data")
        info(f"_proces_data_item data0: {data0}")
        topic: str | None = data.get("topic")
        if not topic:
            warning("no topic found")
            return
        else:
            info(f"topic: '{topic}'")

        self.topic_content[topic] = data0
        await self._dispatch_data_item(topic, item)

    def make_broadcast_clip(self,
                            data: dict | list[dict],
                            meta: dict | None = None,
                            ts: Any = None) -> dict[str, object]:
        """Create a broadcast/clip envelope with DataItem(s)."""
        return make_dict(
            type="broadcast",
            action="clip",
            data=data,
            meta=meta or {},
            ts=ts or utc_timestamp(),
        )

    async def _dispatch_data_item(self, topic: str, item: dict[str, object]):
        subs: set[Connection] | None = self.subs.get(topic)
        info(f"subs: {subs}")
        if not subs:
            return
        payload: dict[str,
                      object] = self.make_broadcast_clip(data=item.get("data"),
                                                         meta=item.get("meta"))
        source = item.get("data", {}).get("source")
        info(f"dispatch source: {source} payload: {payload}")
        for conn in subs:
            if source is not None and conn is source:
                continue
            await conn.enqueue_topic_data(payload)

    async def _proces_put_item(self, item: dict[str, object]):
        # Update content store and fan-out
        await self._proces_data_item(item)

    async def _proces_get_item(self, subject: str, item: dict[str, object]):
        try:
            fut: asyncio.Future = item.get("future")
            info(f"fut: {fut}")
            assert fut
            content = {"message": "none"}
            if subject == "topic":
                topic = item.get("data")
                info(f"topic: {topic}")
                assert topic
                content = self.topic_content.get(topic)
                info(
                    f"found content for topic: {content} from:\n{self.topic_content}"
                )
            elif subject == "topics":
                content = self.topic_content.keys()
            else:
                content = {
                    "message": f"unsupported request subject: '{subject}'"
                }
            trace(f"set result to content: '{content}'")
            fut.set_result(content)
            trace(f"set result to content: '{content}' done")
        except Exception as e:
            error(f'set exception: {type(e)}')
            fut.set_exception(e)

    async def process_queue(self):
        info("wait for item")
        try:
            item = await self.bus.get()
            info(f"got item: {item}")
            self.bus.task_done()
            action: str = item.get("action")
            if action and action == "put":
                info(f"put item: {item}")
                await self._proces_put_item(item)
            elif action and action.startswith("get:"):
                _, subject = action.split(":")
                trace(f"get subject: {subject}")
                await self._proces_get_item(subject, item)
            else:
                raise RuntimeError(f"unknown action: {action}")
        except Exception as e:
            error(f"Exception '{type(e)}'")

        print("process_queue: done.. ")

    async def enqueue_request(self, action: str, data=None):
        loop = self.dispatcher_task.get_loop()
        fut = loop.create_future()
        trace("put request")
        await self.bus.put({"action": action, "data": data, "future": fut})
        trace("wait for future")

        return await fut

    async def enqueue_topic_data(self, data):
        await self.bus.put({"action": "put", "data": data})

    async def enqueue_topic_data_nowait(self, data):
        await self.bus.put_nowait({"action": "put", "data": data})


def subsctibe_client(app: FastAPI, client: Connection, topics: list[str]):
    app.state.main.subsctibe_client(client, topics)


def register_client(app: FastAPI, client: Connection):
    app.state.main.register_client(client)


def unregister_client(app: FastAPI, client: Connection):
    app.state.main.unregister_client(client)


async def enqueue_request_topics(app: FastAPI):
    return await app.state.main.enqueue_request("get:topics")


async def enqueue_request_topic(app: FastAPI, topic):
    return await app.state.main.enqueue_request("get:topic", topic)


async def enqueue_topic_request(app: FastAPI, topic):
    return await enqueue_request_topic(app, topic)


async def enqueue_topic_data(app: FastAPI, data):
    info(f"data: {data}")
    return await app.state.main.enqueue_topic_data(data)


async def enqueue_topic_data_nowait(app: FastAPI, data):
    return await app.state.main.enqueue_topic_data_nowait(data)
