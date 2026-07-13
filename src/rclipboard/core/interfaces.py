"""Runtime participant interfaces and the per-subscriber drainer machinery."""
import asyncio as a
import contextlib
import logging
from abc import ABC, abstractmethod
from typing import override

from rclipboard.models.wire import TopicData


class Interface(ABC):

    def __init__(self):
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @override
    def __repr__(self) -> str:
        return f"Interface[{self.name}]"


_logger = logging.getLogger(__name__)


class BidirectionalInterface(Interface):

    def __init__(self):
        super().__init__()
        self.topics: set[str] = set()
        self._pending: dict[str, "TopicData"] = {}
        self._pending_event: a.Event = a.Event()
        self._drainer_task: a.Task | None = None

    def deliver(self, data: "TopicData") -> None:
        """Enqueue latest value per topic — older pending values are replaced."""
        self._pending[data.topic] = data
        self._pending_event.set()

    async def _run_drainer(self) -> None:
        while True:
            await self._pending_event.wait()
            self._pending_event.clear()
            pending, self._pending = self._pending, {}
            for data in pending.values():
                try:
                    await self.send(data)
                except a.CancelledError:
                    raise
                except Exception as exc:
                    _logger.error("send to %s failed: %s",
                                  self.name,
                                  exc,
                                  exc_info=True)

    def start_drainer(self) -> None:
        if self._drainer_task is None or self._drainer_task.done():
            self._drainer_task = a.create_task(self._run_drainer(),
                                               name=f"drainer_{self.name}")

    async def stop_drainer(self) -> None:
        if self._drainer_task:
            self._drainer_task.cancel()
            with contextlib.suppress(a.CancelledError):
                await self._drainer_task
            self._drainer_task = None

    @abstractmethod
    async def send(self, data: "TopicData") -> None:
        pass
