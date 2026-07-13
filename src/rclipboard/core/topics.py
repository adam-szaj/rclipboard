"""Server-internal wrapper around a stored TopicData item."""
import datetime
import time
from abc import ABC

from rclipboard.core.interfaces import Interface
from rclipboard.models.wire import TopicData


class InternalTopicData(ABC):

    def __init__(self, data: TopicData, source: Interface | None,
                 monitor_conn_id: str | None = None,
                 monitor_app: str | None = None,
                 compare_ts: "datetime.datetime | None" = None):
        self.data: TopicData = data
        self.source: Interface | None = source
        self.stored_at: float = time.monotonic()
        self.stored_at_utc: str = data.meta.get("ts", "")  # type: ignore[assignment]
        self.monitor_conn_id: str | None = monitor_conn_id
        self.monitor_app: str | None = monitor_app
        # Timestamp used for conflict resolution ("newer wins"), already
        # normalised to *this* host's clock when the item arrived from a peer
        # with a known clock offset. ``None`` when no comparable ts exists.
        self.compare_ts: "datetime.datetime | None" = compare_ts
        # Set True by the dispatcher when conflict resolution drops this item
        # (older or losing a tie), so callers can tell it was not stored.
        self.rejected: bool = False

    @property
    def is_remote(self) -> bool:
        """True when this item originated from a remote peer (proxy upstream).

        Used for tie-breaking: on equal timestamps a local value supersedes a
        remote one. Detected via a duck-typed marker on the source interface to
        avoid importing the proxy module (circular import).
        """
        return bool(getattr(self.source, "is_remote_source", False))

    @property
    def topic(self) -> str:
        return self.data.topic
