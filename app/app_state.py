from fastapi import FastAPI
from typing import Any
from abc import ABC, abstractmethod
import asyncio

from logging import Logger
from .log import get_logger

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


class AppState:

    def __init__(self, app: FastAPI):
        self.bus: Bus = Bus()
        self.topic_content: dict[str, object] = {}
        self.subs: dict[str, set[Connection]] = {}
        self.dispatcher_task = asyncio.create_task(self.dispatcher(),
                                                   name="dispatcher")

    async def dispatcher(self):
        while True:
            try:
                await self.process_queue()
            except RuntimeError as e:
                error(f"fail: {e}")

    async def _proces_data_item(self, data_item: dict[str, object],
                                meta: dict[str, object], source: str):
        topic: str = str(data_item.get("topic"))
        if not topic:
            return

        self.topic_content[topic] = {**data_item}
        await self._dispatch_data_item(topic, data_item, meta, source)

    async def _dispatch_data_item(self, topic: str, data_item: dict[str,
                                                                    object],
                                  meta: dict[str, object], source: object):

        subs: set[Connection] | None = self.subs.get(topic)
        if not subs:
            return
        payload: dict[str, object] = make_broadcast_publish(data_item,
                                                            meta=meta)
        for conn in subs:
            if source is not None and conn is source:
                continue
            await conn.enqueue_topic_data(payload)

    async def _proces_put_item(self, item: dict[str, object]):
        data_items = item.get("data").get("data_items", [])
        if not isinstance(data_items, list):
            return

        meta = item.get("meta", {})
        source: object = item.get("source")

        # Update content store and fan-out
        for di in data_items:
            await self._proces_data_item(di, meta, source)

    async def _proces_get_item(self, subject: str, item: dict[str, object]):
        try:
            fut: asyncio.Future = item.get("future")
            assert fut
            content = {"message": "none"}
            if subject == "topic":
                topic = item.get("data")
                assert topic
                content = self.topic_content.get(topic, {})
                # info(f"found content for topic: {content}")
            elif subject == "topics":
                content = self.topic_content.keys()
            else:
                content = {
                    "message": f"unsupported request subject: '{subject}'"
                }
            # trace(f"set result to content: '{content}'")
            fut.set_result(content)
            # trace(f"set result to content: '{content}' done")
        except Exception as e:
            fut.set_exception(e)

    async def process_queue(self):
        # info("wait for item")
        try:
            item = await self.bus.get()
            self.bus.task_done()
            action: str = item.get("action")
            if action and action == "put":
                await self._proces_put_item(item)
            elif action and action.startswith("get:"):
                _, subject = action.split(":")
                trace(f"get subject: {subject}")
                await self._proces_get_item(subject, item)
            else:
                raise RuntimeError(f"unknown action: {action}")
        except Exception as e:
            error(f"Exception {e}")

        # print("process_queue: done.. ")

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


async def enqueue_request_topics(app: FastAPI):
    return await app.state.main.enqueue_request("get:topics")


async def enqueue_request_topic(app: FastAPI, topic):
    return await app.state.main.enqueue_request("get:topic", topic)


async def enqueue_topic_request(app: FastAPI, topic):
    return await enqueue_request_topic(app, topic)


async def enqueue_topic_data(app: FastAPI, data):
    return await app.state.main.enqueue_topic_data(data)


async def enqueue_topic_data_nowait(app: FastAPI, data):
    return await app.state.main.enqueue_topic_data_nowait(data)
