"""Serial-call command objects drained sequentially by the dispatcher.

Each command carries its own result future; the dispatcher pulls it off the
``AppState`` queue, ``call()``s it, and the awaiting requester wakes on the
future. Running everything through one queue serialises access to the topic
store without locks.
"""
import asyncio
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Generic, TypeVar, override

from rclipboard.core.interfaces import Interface
from rclipboard.core.topics import InternalTopicData

if TYPE_CHECKING:
    from rclipboard.core.state import AppState

T = TypeVar("T")


class SerialCall(ABC, Generic[T]):

    def __init__(self):
        self._future: asyncio.Future[T] = asyncio.Future()

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

    def __init__(self, topic_data: InternalTopicData):
        super().__init__()
        self.topic_data: InternalTopicData = topic_data

    @override
    async def do_call(self, app: "AppState") -> None:
        await app.process_put_item(self.topic_data)


class GetTopicData(SerialCall["InternalTopicData | None"]):

    def __init__(self, topic: str, requester: "Interface | None" = None):
        super().__init__()
        self.topic: str = topic
        self.requester: "Interface | None" = requester

    @override
    async def do_call(self, app: "AppState") -> "InternalTopicData | None":
        return app.get_topic_item(self.topic, self.requester)


class GetTopics(SerialCall[list[str]]):

    def __init__(self, requester: "Interface | None" = None):
        super().__init__()
        self.requester: "Interface | None" = requester

    @override
    async def do_call(self, app: "AppState") -> list[str]:
        return app.get_topic_list(self.requester)


GenericSerialCall = GetTopicData | GetTopics | SetTopicData
