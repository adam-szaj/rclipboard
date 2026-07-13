"""Unit tests for time-based conflict resolution ("newer wins; tie → local").

Covers:
- helpers.parse_utc_timestamp
- AppState._accept_incoming decision matrix
- RPCHandler / ProxyClient clock-offset normalisation
"""
from __future__ import annotations

import datetime
import unittest
from unittest import mock

from rclipboard.timeutil import parse_utc_timestamp
from rclipboard.core.topics import InternalTopicData
from rclipboard.models.wire import TopicData, ValueData


UTC = datetime.timezone.utc


def _td(topic: str, value: str, ts: str | None = None) -> TopicData:
    meta: dict = {}
    if ts is not None:
        meta["ts"] = ts
    return TopicData(
        topic=topic,
        value=ValueData(value=value, type="text", encoding="plain"),
        meta=meta,
    )


class _FakeRemoteSource:
    """Stands in for a ProxyClient — carries the remote marker."""
    is_remote_source = True

    @property
    def name(self) -> str:
        return "fake-proxy"


class _FakeLocalSource:
    is_remote_source = False

    @property
    def name(self) -> str:
        return "fake-local"


def _itd(topic: str, value: str, *, ts: datetime.datetime | None,
         remote: bool) -> InternalTopicData:
    src = _FakeRemoteSource() if remote else _FakeLocalSource()
    itd = InternalTopicData(data=_td(topic, value), source=src, compare_ts=ts)
    return itd


class ParseUtcTimestampTests(unittest.TestCase):
    def test_none_and_empty(self):
        self.assertIsNone(parse_utc_timestamp(None))
        self.assertIsNone(parse_utc_timestamp(""))
        self.assertIsNone(parse_utc_timestamp(123))  # non-str

    def test_unparseable(self):
        self.assertIsNone(parse_utc_timestamp("not-a-timestamp"))

    def test_aware(self):
        dt = parse_utc_timestamp("2026-06-03T12:00:00+00:00")
        self.assertIsNotNone(dt)
        assert dt is not None
        self.assertEqual(dt.tzinfo, UTC)

    def test_naive_assumed_utc(self):
        dt = parse_utc_timestamp("2026-06-03T12:00:00")
        assert dt is not None
        self.assertEqual(dt.tzinfo, UTC)


class IsRemoteTests(unittest.TestCase):
    def test_remote_marker(self):
        self.assertTrue(_itd("c", "x", ts=None, remote=True).is_remote)

    def test_local_marker(self):
        self.assertFalse(_itd("c", "x", ts=None, remote=False).is_remote)

    def test_no_source_is_local(self):
        itd = InternalTopicData(data=_td("c", "x"), source=None)
        self.assertFalse(itd.is_remote)


class AcceptIncomingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # AppState.__init__ creates a dispatcher task → needs a running loop.
        from rclipboard.core.state import AppState
        app = mock.MagicMock()
        self.state = AppState(app)

    async def asyncTearDown(self):
        self.state.dispatcher_task.cancel()
        import contextlib
        import asyncio
        with contextlib.suppress(asyncio.CancelledError):
            await self.state.dispatcher_task

    def _accept(self, incoming, existing) -> bool:
        return self.state._accept_incoming(incoming, existing)

    def test_no_existing_accepts(self):
        inc = _itd("c", "new", ts=_dt(10), remote=False)
        self.assertTrue(self._accept(inc, None))

    def test_newer_wins(self):
        old = _itd("c", "old", ts=_dt(10), remote=False)
        new = _itd("c", "new", ts=_dt(20), remote=True)
        self.assertTrue(self._accept(new, old))

    def test_older_loses(self):
        existing = _itd("c", "cur", ts=_dt(20), remote=False)
        incoming = _itd("c", "stale", ts=_dt(10), remote=True)
        self.assertFalse(self._accept(incoming, existing))

    def test_same_side_local_arrival_order_wins(self):
        """Messages from one host count in arrival order — timestamps only
        arbitrate between different hosts. A rapid local succession (same
        clock!) must never be dropped as a 'tie'."""
        existing = _itd("c", "cur", ts=_dt(10), remote=False)
        incoming = _itd("c", "other", ts=_dt(10), remote=False)
        self.assertTrue(self._accept(incoming, existing))

    def test_same_side_remote_arrival_order_wins(self):
        existing = _itd("c", "cur", ts=_dt(10), remote=True)
        incoming = _itd("c", "other", ts=_dt(10), remote=True)
        self.assertTrue(self._accept(incoming, existing))

    def test_same_side_local_accepts_even_older_ts(self):
        # Arrival order rules within one host regardless of stamped ts.
        existing = _itd("c", "cur", ts=_dt(20), remote=False)
        incoming = _itd("c", "late-stamped-old", ts=_dt(10), remote=False)
        self.assertTrue(self._accept(incoming, existing))

    def test_proxied_put_vs_local_put_uses_timestamps(self):
        """At the upstream both items arrive on 'local' interfaces, but a put
        forwarded by a proxy (meta via/host) originated on a DIFFERENT host —
        timestamps must arbitrate, not arrival order."""
        proxied = _td("c", "from-proxy")
        proxied.meta.update({"via": "proxy", "host": "laptop"})
        existing = InternalTopicData(data=proxied, source=_FakeLocalSource(),
                                     compare_ts=_dt(20))
        incoming = _itd("c", "older-direct", ts=_dt(10), remote=False)
        self.assertFalse(self._accept(incoming, existing))

    def test_two_puts_from_same_proxied_host_arrival_order(self):
        def _proxied(value: str, ts) -> InternalTopicData:
            td = _td("c", value)
            td.meta.update({"via": "proxy", "host": "laptop"})
            return InternalTopicData(data=td, source=_FakeLocalSource(),
                                     compare_ts=ts)
        existing = _proxied("first", _dt(20))
        incoming = _proxied("second-older-ts", _dt(10))
        self.assertTrue(self._accept(incoming, existing))

    def test_tie_local_beats_existing_remote(self):
        # remis → wygrywa lokalny
        existing = _itd("c", "remote-cur", ts=_dt(10), remote=True)
        incoming = _itd("c", "local-new", ts=_dt(10), remote=False)
        self.assertTrue(self._accept(incoming, existing))

    def test_tie_remote_does_not_beat_existing_local(self):
        existing = _itd("c", "local-cur", ts=_dt(10), remote=False)
        incoming = _itd("c", "remote-new", ts=_dt(10), remote=True)
        self.assertFalse(self._accept(incoming, existing))

    def test_missing_incoming_ts_accepts(self):
        existing = _itd("c", "cur", ts=_dt(20), remote=False)
        incoming = _itd("c", "no-ts", ts=None, remote=True)
        self.assertTrue(self._accept(incoming, existing))

    def test_missing_existing_ts_accepts(self):
        existing = _itd("c", "cur", ts=None, remote=False)
        incoming = _itd("c", "has-ts", ts=_dt(20), remote=True)
        self.assertTrue(self._accept(incoming, existing))

    def test_within_tie_window_remote_local_local_wins(self):
        # incoming slightly newer but inside the tie window → treated as tie →
        # local supersedes remote.
        eps = self.state.sync_tie_window
        existing = _itd("c", "remote", ts=_dt(10), remote=True)
        incoming = _itd("c", "local",
                        ts=_dt(10) + eps / 2, remote=False)
        self.assertTrue(self._accept(incoming, existing))

    def test_within_tie_window_remote_incoming_loses(self):
        # incoming (remote) slightly newer but inside the window → tie → keep
        # the existing local value.
        eps = self.state.sync_tie_window
        existing = _itd("c", "local", ts=_dt(10), remote=False)
        incoming = _itd("c", "remote",
                        ts=_dt(10) + eps / 2, remote=True)
        self.assertFalse(self._accept(incoming, existing))

    def test_outside_tie_window_newer_wins_even_local_existing(self):
        # genuinely newer (beyond the window) always wins, regardless of side.
        eps = self.state.sync_tie_window
        existing = _itd("c", "local-old", ts=_dt(10), remote=False)
        incoming = _itd("c", "remote-new",
                        ts=_dt(10) + eps + datetime.timedelta(seconds=1),
                        remote=True)
        self.assertTrue(self._accept(incoming, existing))

    def test_outside_tie_window_older_loses(self):
        eps = self.state.sync_tie_window
        existing = _itd("c", "cur", ts=_dt(20), remote=False)
        incoming = _itd("c", "stale",
                        ts=_dt(20) - eps - datetime.timedelta(seconds=1),
                        remote=True)
        self.assertFalse(self._accept(incoming, existing))


def _dt(second: int) -> datetime.datetime:
    return datetime.datetime(2026, 6, 3, 12, 0, second, tzinfo=UTC)


class ProxyNormalizeTsTests(unittest.TestCase):
    """ProxyClient._normalize_peer_ts applies the upstream clock offset."""

    def _client(self, offset_seconds: float | None):
        from rclipboard.transports.proxy import ProxyClient
        c = ProxyClient.__new__(ProxyClient)  # skip __init__ (no event loop)
        c._peer_clock_offset = (
            None if offset_seconds is None
            else datetime.timedelta(seconds=offset_seconds))
        return c

    def test_no_offset_uses_raw(self):
        c = self._client(None)
        td = _td("c", "x", ts="2026-06-03T12:00:10+00:00")
        self.assertEqual(c._normalize_peer_ts(td), _dt(10))

    def test_positive_offset_subtracts(self):
        # upstream clock leads ours by 5s → its ts maps back by -5s
        c = self._client(5)
        td = _td("c", "x", ts="2026-06-03T12:00:10+00:00")
        self.assertEqual(c._normalize_peer_ts(td), _dt(5))

    def test_negative_offset_adds(self):
        c = self._client(-3)
        td = _td("c", "x", ts="2026-06-03T12:00:10+00:00")
        self.assertEqual(c._normalize_peer_ts(td), _dt(13))

    def test_missing_ts_returns_none(self):
        c = self._client(5)
        self.assertIsNone(c._normalize_peer_ts(_td("c", "x", ts=None)))


class HandlerPeerClockTests(unittest.TestCase):
    """RPCHandler records the connecting peer's clock and normalises its ts."""

    def _handler(self):
        from rclipboard.transports.rpc_handler import RPCHandler

        class _ConcreteHandler(RPCHandler):
            @property
            def name(self) -> str:
                return "test-handler"

            async def _send_result(self, request_id, result):  # noqa: D401
                ...

            async def _send_error(self, request_id, rpc_error):
                ...

            async def _send_event(self, method, params):
                ...

        h = _ConcreteHandler.__new__(_ConcreteHandler)
        h._peer_clock_offset = None
        return h

    def test_record_offset_peer_leads(self):
        h = self._handler()
        # peer says 12:00:10 while we are at 12:00:00 → peer leads by 10s
        h._record_peer_clock("2026-06-03T12:00:10+00:00",
                             "2026-06-03T12:00:00+00:00")
        self.assertEqual(h._peer_clock_offset,
                         datetime.timedelta(seconds=10))
        # a ts the peer stamps at 12:00:10 maps to our 12:00:00
        td = _td("c", "x", ts="2026-06-03T12:00:10+00:00")
        self.assertEqual(h._normalize_peer_ts(td), _dt(0))

    def test_record_offset_ignores_bad_input(self):
        h = self._handler()
        h._record_peer_clock("garbage", "2026-06-03T12:00:00+00:00")
        self.assertIsNone(h._peer_clock_offset)

    def test_normalize_without_offset_is_raw(self):
        h = self._handler()
        td = _td("c", "x", ts="2026-06-03T12:00:10+00:00")
        self.assertEqual(h._normalize_peer_ts(td), _dt(10))


if __name__ == "__main__":
    unittest.main()
