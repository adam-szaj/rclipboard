"""Serial-call bus: command objects drained sequentially by the dispatcher."""
import asyncio
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Generic, TypeVar, override

from rclipboard.core.interfaces import Interface
from rclipboard.core.topics import InternalTopicData

if TYPE_CHECKING:
    from rclipboard.core.state import AppState

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
