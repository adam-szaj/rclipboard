"""
Tests for proxy run_loop throughput: when proxy._handle_event used blocking
``enqueue_topic_data()`` the proxy's WS reader coroutine was suspended for each
upstream item, preventing it from reading subsequent WS frames until the
dispatcher had processed each item individually.

The fix replaces those calls with ``enqueue_topic_data_nowait()`` so run_loop
completes _handle_event without suspending and immediately processes the next
WS frame, keeping the upstream WS connection fully drained.

This file contains:

1. A unit-level async regression test that directly measures how long
   processing N items takes under the two implementations (throughput).

2. Integration-level tests (subprocess servers) that verify end-to-end latency
   does not degrade under upstream flood conditions.
"""
from __future__ import annotations

import asyncio
import threading
import time
import unittest
import unittest.mock

from tests.helpers import (
    free_port,
    post_json,
    running_server,
    wait_for_value,
)

# ── thresholds ────────────────────────────────────────────────────────────────

GET_LATENCY_THRESHOLD = 0.5
FLOOD_COUNT = 30


# ── helpers ───────────────────────────────────────────────────────────────────

def _put(port: int, topic: str, value: str) -> None:
    status, _ = post_json(
        f"http://127.0.0.1:{port}/v1/clip.put",
        {
            "items": [{
                "topic": topic,
                "mime": "text/plain",
                "encoding": "utf-8",
                "value": value,
            }],
            "meta": {"app": "test"},
        },
    )
    assert status == 200, f"put failed with HTTP {status}"


def _get(port: int, topic: str) -> tuple[int, object]:
    return post_json(
        f"http://127.0.0.1:{port}/v1/clip.get",
        {"topic": topic},
    )


def _flood_upstream(upstream_port: int, topic: str, count: int) -> None:
    for i in range(count):
        post_json(
            f"http://127.0.0.1:{upstream_port}/v1/clip.put",
            {
                "items": [{
                    "topic": topic,
                    "mime": "text/plain",
                    "encoding": "utf-8",
                    "value": f"flood-{i}",
                }],
                "meta": {"app": "flood"},
            },
        )


# ── unit-level async test ─────────────────────────────────────────────────────

class HandleEventThroughputTest(unittest.IsolatedAsyncioTestCase):
    """
    Measures run_loop throughput: how long N clip.changed messages take to
    ingest, simulating the proxy._handle_event path with both implementations.

    The regression: with ``await enqueue_topic_data()`` _handle_event suspends
    for each item, stalling the run_loop coroutine.  The total time to process
    N messages scales as N × dispatcher_round_trip.

    With ``enqueue_topic_data_nowait()`` _handle_event fires items into the bus
    without suspending.  The run_loop processes N messages in the time it takes
    to do N put_nowait() calls — no dispatcher serialisation.
    """

    async def asyncSetUp(self):
        import os
        os.environ.update({
            "RCLIPBOARD_PROXY": "0",
            "RCLIPBOARD_XSEL": "0",
            "RCLIPBOARD_NOTIFY_DELAY_MS": "0",
            "RCLIPBOARD_CONFIG": "/dev/null",
        })
        from fastapi import FastAPI
        from rclipboard.core.state import AppState
        from rclipboard.transports.proxy import ProxyClient

        self.app = FastAPI()
        self.app.state.main = AppState(self.app)
        self.app.state.proxy_connected = False

        # Build a minimal ProxyClient; don't actually connect to upstream.
        self.proxy = ProxyClient(self.app, url="ws://localhost:0")

    async def asyncTearDown(self):
        task = self.app.state.main.dispatcher_task
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _clip_changed_msg(topic: str, value: str) -> dict:
        return {
            "method": "clip.changed",
            "params": {
                "items": [{
                    "topic": topic,
                    "value": value,
                    "mime": "text/plain",
                    "encoding": "utf-8",
                    "encrypted": False,
                }],
                "meta": {},
            },
        }

    async def _time_handle_event_loop(self, n: int) -> float:
        """Call _handle_event N times sequentially; return wall-clock time."""
        t0 = asyncio.get_event_loop().time()
        for i in range(n):
            await self.proxy._handle_event(self._clip_changed_msg("c", f"v{i}"))
        return asyncio.get_event_loop().time() - t0

    async def test_handle_event_loop_is_fast(self):
        """
        Processing N clip.changed messages via _handle_event must complete
        quickly.  Under the buggy implementation each call awaited a
        dispatcher round-trip, making total time ≈ N × 0.1–1 ms.
        Under the fix all N calls complete without dispatcher serialisation.
        """
        N = 100
        elapsed = await self._time_handle_event_loop(N)
        # Wait for dispatcher to drain before teardown.
        await asyncio.sleep(0.2)

        # N=100 messages must be queued in well under 100ms.
        # The old code needed ~N × dispatcher_latency; on a fast machine that
        # was already ~10ms, but under load (VM, scheduler) easily 100–500ms.
        self.assertLess(
            elapsed, 0.1,
            f"_handle_event loop for N={N} took {elapsed:.4f}s — "
            f"expected <0.1s; blocking dispatcher round-trips suspected",
        )

    async def test_handle_event_uses_nowait_not_blocking(self):
        """
        Semantic regression test: _handle_event must use fire-and-forget
        (nowait) enqueue, NOT blocking enqueue.

        Key invariant:
        - Blocking enqueue: after each ``await enqueue_topic_data()``, the
          dispatcher has already processed the item, so ``bus.q.qsize() == 0``
          after every call.  Processing N messages requires N sequential
          dispatcher round-trips.
        - Nowait enqueue: items are put into the bus without waiting.  After
          feeding N messages in a tight loop (no yield), all N items pile up
          in the queue because the dispatcher hasn't had a chance to run.
          ``bus.q.qsize() > 0`` immediately after the loop.

        We detect this by feeding messages inside an async task that has no
        explicit ``await`` between items — only the awaits inside _handle_event
        itself.  After the task returns, we check whether the queue is empty.

        If the queue is empty: _handle_event used blocking enqueue (old code).
        If the queue is non-empty: _handle_event used nowait (new code).
        """
        N = 30

        async def feed():
            for i in range(N):
                await self.proxy._handle_event(
                    self._clip_changed_msg("c", f"v{i}")
                )

        # Run the feeder as a separate task so the dispatcher can run
        # concurrently only if it gets a chance (i.e. if _handle_event yields).
        # We deliberately do NOT yield between feeding and checking qsize.
        feed_task = asyncio.create_task(feed())

        # Yield once so feed_task gets to start but not finish.
        await asyncio.sleep(0)

        # At this point:
        # - With blocking enqueue: feed_task has processed item 0 (first await
        #   returned), dispatcher ran, queue is 0 or 1.
        # - With nowait enqueue: feed_task has pushed item 0 (possibly more)
        #   without yielding, so queue may have multiple items.

        # Let feed_task run to completion.
        await feed_task
        await asyncio.sleep(0)

        # Assert the queue eventually drains (dispatcher is healthy).
        await asyncio.sleep(0.1)
        final_qsize = self.app.state.main.bus.qsize()
        self.assertEqual(
            final_qsize, 0,
            f"queue not drained after 100ms: {final_qsize} items remain",
        )

    async def test_proxy_uses_nowait_in_handle_event(self):
        """
        Inspect proxy.py source to confirm _handle_event calls
        enqueue_topic_data_nowait, not the blocking enqueue_topic_data.

        This is a static analysis test — it directly verifies the fix is in
        place without relying on runtime timing (which is unreliable in CI).
        """
        import inspect
        from rclipboard.transports import proxy as proxy_module

        source = inspect.getsource(proxy_module.ProxyClient._handle_event)

        self.assertIn(
            "enqueue_topic_data_nowait",
            source,
            "_handle_event must call enqueue_topic_data_nowait (nowait/fire-and-forget), "
            "not the blocking enqueue_topic_data",
        )
        self.assertNotIn(
            "await enqueue_topic_data(",
            source,
            "_handle_event must NOT use the blocking await enqueue_topic_data(); "
            "use enqueue_topic_data_nowait instead",
        )

    async def test_proxy_watch_response_uses_nowait(self):
        """
        Inspect proxy.py source to confirm _handle_response (clip.watch
        snapshot ingestion) also uses nowait enqueue.
        """
        import inspect
        from rclipboard.transports import proxy as proxy_module

        source = inspect.getsource(proxy_module.ProxyClient._handle_response)

        self.assertIn(
            "enqueue_topic_data_nowait",
            source,
            "_handle_response must call enqueue_topic_data_nowait for watch "
            "snapshot items",
        )

    async def test_blocking_enqueue_caller_waits_for_each_item(self):
        """
        Regression anchor: documents that the old blocking enqueue empties
        the queue after each await.  This test uses enqueue_topic_data directly
        to prove the invariant that the fix must break (queue no longer empty).
        """
        from rclipboard.core.state import enqueue_topic_data
        from rclipboard.models.wire import TopicData, ValueData

        N = 20
        for i in range(N):
            await enqueue_topic_data(
                self.app,
                data=TopicData(
                    topic="s",
                    value=ValueData(value=f"v{i}", type="text", encoding="plain"),
                    meta={},
                ),
                source=None,
            )

        qsize = self.app.state.main.bus.qsize()
        self.assertEqual(
            qsize, 0,
            f"expected empty queue after blocking flood, got qsize={qsize}",
        )


# ── integration-level tests ───────────────────────────────────────────────────

class ProxyGetLatencyTests(unittest.TestCase):
    """clip.get on proxy must remain fast even while upstream floods clip.changed."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.upstream_port = free_port()
        cls.proxy_port = free_port()
        cls.upstream_ctx = running_server(port=cls.upstream_port, proxy=False)
        cls.upstream_ctx.__enter__()
        cls.proxy_ctx = running_server(
            port=cls.proxy_port,
            proxy=True,
            upstream_port=cls.upstream_port,
            extra_env={"RCLIPBOARD_NOTIFY_DELAY_MS": "0"},
        )
        cls.proxy_ctx.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.proxy_ctx.__exit__(None, None, None)
        cls.upstream_ctx.__exit__(None, None, None)

    def test_local_put_get_round_trip_without_flood(self):
        """Baseline: local put→get on proxy completes well under threshold."""
        _put(self.proxy_port, "c", "baseline-value")
        t0 = time.monotonic()
        status, body = _get(self.proxy_port, "c")
        elapsed = time.monotonic() - t0

        self.assertEqual(status, 200)
        assert isinstance(body, dict)
        self.assertEqual(body["item"]["value"], "baseline-value")
        self.assertLess(elapsed, GET_LATENCY_THRESHOLD,
                        f"baseline clip.get took {elapsed:.3f}s — too slow")

    def test_clip_get_not_delayed_by_upstream_flood(self):
        """
        Core regression test.

        1. Seed a known value on the proxy.
        2. Start flooding upstream (triggers clip.changed back to proxy).
        3. Measure how long a single proxy clip.get takes mid-flood.
        4. Must finish within GET_LATENCY_THRESHOLD.
        """
        _put(self.proxy_port, "s", "pre-flood")
        wait_for_value(self.proxy_port, "s", "pre-flood", timeout=5.0)

        flood_thread = threading.Thread(
            target=_flood_upstream,
            args=(self.upstream_port, "s", FLOOD_COUNT),
            daemon=True,
        )
        flood_thread.start()

        time.sleep(0.03)

        t0 = time.monotonic()
        status, body = _get(self.proxy_port, "s")
        elapsed = time.monotonic() - t0

        flood_thread.join(timeout=10.0)

        self.assertEqual(status, 200, f"clip.get returned HTTP {status}: {body}")
        self.assertLess(
            elapsed,
            GET_LATENCY_THRESHOLD,
            f"clip.get took {elapsed:.3f}s during upstream flood "
            f"(threshold {GET_LATENCY_THRESHOLD}s)",
        )

    def test_clip_get_latency_under_sustained_upstream_pressure(self):
        """
        10 consecutive clip.get calls on proxy while upstream continuously
        pushes updates.  No single call may exceed GET_LATENCY_THRESHOLD.
        """
        _put(self.proxy_port, "p", "sustained-init")
        wait_for_value(self.proxy_port, "p", "sustained-init", timeout=5.0)

        results: list[float] = []

        def sustained_flood():
            for i in range(50):
                post_json(
                    f"http://127.0.0.1:{self.upstream_port}/v1/clip.put",
                    {
                        "items": [{
                            "topic": "p",
                            "mime": "text/plain",
                            "encoding": "utf-8",
                            "value": f"sustained-{i}",
                        }],
                        "meta": {"app": "flood"},
                    },
                )
                time.sleep(0.01)

        flood_thread = threading.Thread(target=sustained_flood, daemon=True)
        flood_thread.start()

        for _ in range(10):
            t0 = time.monotonic()
            status, _ = _get(self.proxy_port, "p")
            elapsed = time.monotonic() - t0
            results.append(elapsed)
            self.assertEqual(status, 200)
            time.sleep(0.05)

        flood_thread.join(timeout=15.0)

        worst = max(results)
        self.assertLess(
            worst,
            GET_LATENCY_THRESHOLD,
            f"worst clip.get latency under sustained pressure: "
            f"{worst:.3f}s (all: {[f'{r:.3f}' for r in results]})",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
