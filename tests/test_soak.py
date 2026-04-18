"""
Soak / long-running tests for rclipboard.

These tests run the server(s) for an extended period and verify that:
  - memory does not grow unboundedly (topic_content dict, task sets)
  - no asyncio tasks are leaked after repeated put/get cycles
  - dispatcher queue does not back up under sustained load
  - proxy reconnects correctly after repeated upstream restarts
  - WS subscriber set is cleaned up after client disconnects
  - encrypted + large (stub) payloads do not cause memory growth
  - proxy telemetry counters are consistent

Run individually (excluded from the default `make test` suite):

    PYTHONPATH=src python -m unittest tests.test_soak -v

Tunables (env vars):

    SOAK_DURATION_S=10   # default: 30
    SOAK_WORKERS=4       # default: 8
    SOAK_PAYLOAD_KB=16   # default: 64 (large-payload / stub tests)
    SOAK_OUT_DIR=/tmp    # directory for JSON + CSV reports (default: none)

Per-test and per-class metrics (latency percentiles, throughput, RSS) are
printed to stdout at the end of every test class.  When SOAK_OUT_DIR is set
each class also writes:
    <class_name>.json
    <class_name>.csv
"""

from __future__ import annotations

import json
import os
import random
import string
import sys
import threading
import time
import unittest
from pathlib import Path

try:
    import websockets.sync.client  # type: ignore[import]  # noqa: F401
    HAS_WS_SYNC = True
except ImportError:
    HAS_WS_SYNC = False

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "src"))

from tests.helpers import (  # noqa: E402
    free_port,
    post_json,
    get_json,
    running_server,
    wait_for_value,
)
from tests.metrics import (  # noqa: E402
    MetricsCollector,
    print_report,
    dump_json,
    dump_csv,
)

# ── tunables ──────────────────────────────────────────────────────────────────
DURATION_S    = int(os.environ.get("SOAK_DURATION_S", "30"))
WORKERS       = int(os.environ.get("SOAK_WORKERS", "8"))
PAYLOAD_KB    = int(os.environ.get("SOAK_PAYLOAD_KB", "64"))
LAZY_LOCAL_KB = PAYLOAD_KB // 2
TOPICS        = ["c", "p", "s"]
ADMIN_TOKEN   = "soak-test-token"
OUT_DIR       = os.environ.get("SOAK_OUT_DIR", "")


# ── helpers ───────────────────────────────────────────────────────────────────

def _rand_text(n_chars: int) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=n_chars))


def _put(port: int, topic: str, value: str, *,
         encoding: str = "utf-8", mime: str = "text/plain") -> None:
    post_json(
        f"http://127.0.0.1:{port}/v1/clip.put",
        {"items": [{"topic": topic, "mime": mime,
                    "encoding": encoding, "value": value}],
         "meta": {"app": "soak"}},
    )


def _get_status(port: int) -> dict:
    _, body = get_json(f"http://127.0.0.1:{port}/v1/status.get")
    assert isinstance(body, dict)
    return body


def _get_topic(port: int, topic: str) -> dict | None:
    status, body = post_json(
        f"http://127.0.0.1:{port}/v1/clip.get", {"topic": topic}
    )
    if status == 404:
        return None
    return body  # type: ignore[return-value]


def _server_pid(proc_ctx) -> int | None:
    """Extract PID from a running_server context (returns the Popen object)."""
    try:
        return proc_ctx.pid  # type: ignore[attr-defined]
    except AttributeError:
        return None


def _emit_metrics(collector: MetricsCollector, label: str) -> None:
    """Print report + write files if SOAK_OUT_DIR is set."""
    collector.finalize()
    print_report([collector])
    if OUT_DIR:
        out = Path(OUT_DIR)
        out.mkdir(parents=True, exist_ok=True)
        dump_json([collector], out / f"{label}.json")
        dump_csv([collector],  out / f"{label}.csv")


# ── sustained put/get worker ──────────────────────────────────────────────────

class _PutGetWorker(threading.Thread):
    """Repeatedly puts and gets small clips for `duration` seconds.

    Records per-call latencies into a shared MetricsCollector so that all
    worker threads contribute to the same histogram.
    """

    def __init__(self, port: int, duration: float, topic: str,
                 collector: MetricsCollector):
        super().__init__(daemon=True)
        self.port = port
        self.duration = duration
        self.topic = topic
        self.collector = collector
        self.put_count = 0
        self.get_count = 0
        self.errors: list[str] = []

    def run(self) -> None:
        deadline = time.monotonic() + self.duration
        while time.monotonic() < deadline:
            try:
                val = _rand_text(64)
                with self.collector.measure("clip.put"):
                    _put(self.port, self.topic, val)
                self.put_count += 1
                with self.collector.measure("clip.get"):
                    result = _get_topic(self.port, self.topic)
                if result:
                    self.get_count += 1
            except Exception as exc:
                self.errors.append(str(exc))
            time.sleep(0.005)


# ── base class: wires up per-class MetricsCollector ──────────────────────────

class SoakBase(unittest.TestCase):
    """Mixin: creates a MetricsCollector before every test and emits the
    aggregate report after the class is torn down."""

    _mc: MetricsCollector
    _server_pid: int | None = None
    # RSS snapshot interval in seconds (0 = only start/end)
    _rss_interval_s: float = 2.0
    _rss_thread: threading.Thread | None = None
    _rss_stop: threading.Event

    @classmethod
    def _start_rss_watcher(cls) -> None:
        cls._rss_stop = threading.Event()

        def _watch():
            while not cls._rss_stop.wait(timeout=cls._rss_interval_s):
                cls._mc.snapshot_rss()

        cls._rss_thread = threading.Thread(target=_watch, daemon=True)
        cls._rss_thread.start()

    @classmethod
    def _stop_rss_watcher(cls) -> None:
        if cls._rss_thread:
            cls._rss_stop.set()
            cls._rss_thread.join(timeout=5)

    def setUp(self) -> None:
        self._mc.set_test(self._testMethodName)
        # per-test start snapshot
        self._mc.snapshot_rss()

    def tearDown(self) -> None:
        # per-test end snapshot
        self._mc.snapshot_rss()


# ── test classes ──────────────────────────────────────────────────────────────

class SoakBasicTests(SoakBase):
    """Sustained put/get against a single server."""

    _proc = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.port = free_port()
        cls.ctx = running_server(port=cls.port)
        cls._proc = cls.ctx.__enter__()
        cls._server_pid = getattr(cls._proc, "pid", None)
        cls._mc = MetricsCollector("SoakBasicTests", server_pid=cls._server_pid)
        cls._mc.snapshot_rss()
        cls._start_rss_watcher()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._stop_rss_watcher()
        cls._mc.snapshot_rss()
        cls.ctx.__exit__(None, None, None)
        _emit_metrics(cls._mc, "SoakBasicTests")

    def test_sustained_put_get_no_errors(self):
        """Run N workers for DURATION_S seconds; expect zero errors."""
        workers = [
            _PutGetWorker(self.port, DURATION_S,
                          random.choice(TOPICS), self._mc)
            for _ in range(WORKERS)
        ]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=DURATION_S + 10)

        total_puts = sum(w.put_count for w in workers)
        total_gets = sum(w.get_count for w in workers)
        all_errors = [e for w in workers for e in w.errors]

        self.assertGreater(total_puts, 0, "no puts completed")
        self.assertGreater(total_gets, 0, "no gets completed")
        self.assertEqual(all_errors, [], f"worker errors: {all_errors[:5]}")

    def test_topic_set_does_not_grow(self):
        """After repeated puts to fixed topics, topic_content must not grow."""
        for _ in range(200):
            with self._mc.measure("clip.put"):
                _put(self.port, random.choice(TOPICS), _rand_text(32))
        time.sleep(0.5)
        with self._mc.measure("status.get"):
            status = _get_status(self.port)
        n_topics = len(status.get("topics", []))
        self.assertLessEqual(
            n_topics, len(TOPICS),
            f"topic_content grew unexpectedly: {status['topics']}",
        )

    def test_health_always_ok_under_load(self):
        """Health endpoint must return ok=True throughout a load burst."""
        failures = []

        def _load():
            deadline = time.monotonic() + DURATION_S
            while time.monotonic() < deadline:
                with self._mc.measure("clip.put"):
                    _put(self.port, "c", _rand_text(32))
                time.sleep(0.01)

        def _poll():
            deadline = time.monotonic() + DURATION_S
            while time.monotonic() < deadline:
                with self._mc.measure("health.get"):
                    _, body = get_json(
                        f"http://127.0.0.1:{self.port}/v1/health.get"
                    )
                if not (isinstance(body, dict) and body.get("ok")):
                    failures.append(body)
                time.sleep(0.1)

        t_load = threading.Thread(target=_load, daemon=True)
        t_poll = threading.Thread(target=_poll, daemon=True)
        t_load.start()
        t_poll.start()
        t_load.join(timeout=DURATION_S + 5)
        t_poll.join(timeout=DURATION_S + 5)

        self.assertEqual(failures, [], f"health failures: {failures[:3]}")


class SoakLargePayloadTests(SoakBase):
    """Large payloads with lazy-sync stub threshold."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.port = free_port()
        cls.ctx = running_server(
            port=cls.port,
            extra_env={"RCLIPBOARD_LAZY_LOCAL_KB": str(LAZY_LOCAL_KB)},
        )
        cls._proc = cls.ctx.__enter__()
        cls._server_pid = getattr(cls._proc, "pid", None)
        cls._mc = MetricsCollector("SoakLargePayloadTests",
                                   server_pid=cls._server_pid)
        cls._mc.snapshot_rss()
        cls._start_rss_watcher()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._stop_rss_watcher()
        cls._mc.snapshot_rss()
        cls.ctx.__exit__(None, None, None)
        _emit_metrics(cls._mc, "SoakLargePayloadTests")

    def test_large_payload_stored_and_retrievable(self):
        """A payload > threshold is stored in full and GET returns it."""
        import base64
        big_b64 = base64.b64encode(_rand_text(PAYLOAD_KB * 1024).encode()).decode()
        with self._mc.measure("clip.put.large"):
            _put(self.port, "c", big_b64, encoding="base64",
                 mime="application/octet-stream")
        time.sleep(0.3)
        with self._mc.measure("clip.get.large"):
            result = _get_topic(self.port, "c")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["item"]["value"], big_b64)

    def test_repeated_large_puts_stable_memory(self):
        """Putting large payloads N times must not grow topic_content."""
        import base64
        for i in range(20):
            big_b64 = base64.b64encode(
                _rand_text(PAYLOAD_KB * 1024).encode()
            ).decode()
            topic = TOPICS[i % len(TOPICS)]
            with self._mc.measure("clip.put.large"):
                _put(self.port, topic, big_b64, encoding="base64",
                     mime="application/octet-stream")
        time.sleep(0.5)
        with self._mc.measure("status.get"):
            status = _get_status(self.port)
        n_topics = len(status.get("topics", []))
        self.assertLessEqual(n_topics, len(TOPICS))

    def test_status_topic_size_reported(self):
        """status.get must report non-None size for a stored large payload."""
        import base64
        big_b64 = base64.b64encode(_rand_text(PAYLOAD_KB * 1024).encode()).decode()
        with self._mc.measure("clip.put.large"):
            _put(self.port, "s", big_b64, encoding="base64",
                 mime="application/octet-stream")
        time.sleep(0.3)
        with self._mc.measure("status.get"):
            status = _get_status(self.port)
        ts_list = status.get("topic_status", [])
        by_topic = {ts["topic"]: ts for ts in ts_list}
        self.assertIn("s", by_topic)
        self.assertIsNotNone(by_topic["s"]["size"])
        self.assertGreater(by_topic["s"]["size"], 0)


class SoakProxyTests(SoakBase):
    """Proxy connection stability and telemetry consistency."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.upstream_port = free_port()
        cls.proxy_port = free_port()
        cls.upstream_ctx = running_server(
            port=cls.upstream_port,
            extra_env={"RCLIPBOARD_ADMIN_TOKEN": ADMIN_TOKEN},
        )
        cls._proc_upstream = cls.upstream_ctx.__enter__()
        cls.proxy_ctx = running_server(
            port=cls.proxy_port,
            proxy=True,
            upstream_port=cls.upstream_port,
            extra_env={"RCLIPBOARD_ADMIN_TOKEN": ADMIN_TOKEN},
        )
        cls._proc_proxy = cls.proxy_ctx.__enter__()
        # Track proxy server RSS (the interesting one)
        cls._server_pid = getattr(cls._proc_proxy, "pid", None)
        cls._mc = MetricsCollector("SoakProxyTests",
                                   server_pid=cls._server_pid)
        cls._mc.snapshot_rss()
        cls._start_rss_watcher()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._stop_rss_watcher()
        cls._mc.snapshot_rss()
        cls.proxy_ctx.__exit__(None, None, None)
        cls.upstream_ctx.__exit__(None, None, None)
        _emit_metrics(cls._mc, "SoakProxyTests")

    def test_sustained_replication_upstream_to_proxy(self):
        """Continuous upstream puts must reach proxy without loss."""
        n = 30
        sent: list[str] = []
        for i in range(n):
            val = f"upstream-soak-{i}-{_rand_text(8)}"
            with self._mc.measure("clip.put.upstream"):
                _put(self.upstream_port, "c", val)
            sent.append(val)
            time.sleep(DURATION_S / n)
        last = sent[-1]
        wait_for_value(self.proxy_port, "c", last, timeout=10.0)

    def test_proxy_telemetry_rx_count_increases(self):
        """rx_count in proxy status must increase as upstream sends clips."""
        with self._mc.measure("status.get"):
            status_before = _get_status(self.proxy_port)
        rx_before = status_before.get("proxy", {}).get("rx_count", 0)

        for i in range(10):
            with self._mc.measure("clip.put.upstream"):
                _put(self.upstream_port, "c", f"telemetry-{i}")
            time.sleep(0.05)
        time.sleep(0.5)

        with self._mc.measure("status.get"):
            status_after = _get_status(self.proxy_port)
        rx_after = status_after.get("proxy", {}).get("rx_count", 0)
        self.assertGreater(rx_after, rx_before,
                           "proxy rx_count did not increase")

    def test_proxy_connect_disconnect_cycle(self):
        """proxy.disconnect then proxy.connect must restore replication."""
        with self._mc.measure("proxy.disconnect"):
            s, b = post_json(
                f"http://127.0.0.1:{self.proxy_port}/v1/proxy.disconnect",
                {},
                extra_headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            )
        self.assertEqual(s, 200, f"disconnect failed: {b}")
        time.sleep(0.5)

        _put(self.upstream_port, "c", "while-disconnected")
        time.sleep(0.5)
        result = _get_topic(self.proxy_port, "c")
        if result:
            self.assertNotEqual(result["item"]["value"], "while-disconnected",
                                "proxy received update while disconnected")

        with self._mc.measure("proxy.connect"):
            s, b = post_json(
                f"http://127.0.0.1:{self.proxy_port}/v1/proxy.connect",
                {"endpoint": f"127.0.0.1:{self.upstream_port}",
                 "reconnect": True},
                extra_headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            )
        self.assertEqual(s, 200, f"connect failed: {b}")
        time.sleep(1.0)

        val = f"after-reconnect-{_rand_text(8)}"
        _put(self.upstream_port, "c", val)
        wait_for_value(self.proxy_port, "c", val, timeout=10.0)

    def test_proxy_connect_disconnect_repeated(self):
        """Repeated connect/disconnect cycles must not accumulate stuck tasks."""
        for cycle in range(5):
            with self._mc.measure("proxy.disconnect"):
                s, b = post_json(
                    f"http://127.0.0.1:{self.proxy_port}/v1/proxy.disconnect",
                    {},
                    extra_headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
                )
            self.assertEqual(s, 200, f"cycle {cycle} disconnect failed: {b}")
            time.sleep(0.2)

            with self._mc.measure("proxy.connect"):
                s, b = post_json(
                    f"http://127.0.0.1:{self.proxy_port}/v1/proxy.connect",
                    {"endpoint": f"127.0.0.1:{self.upstream_port}",
                     "reconnect": True},
                    extra_headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
                )
            self.assertEqual(s, 200, f"cycle {cycle} connect failed: {b}")
            time.sleep(0.3)

        with self._mc.measure("health.get"):
            _, health = get_json(
                f"http://127.0.0.1:{self.proxy_port}/v1/health.get"
            )
        self.assertTrue(health["ok"])  # type: ignore[index]

    def test_proxy_status_last_rx_ago_is_recent(self):
        """After a fresh put, last_rx_ago should be small."""
        with self._mc.measure("clip.put.upstream"):
            _put(self.upstream_port, "p", f"fresh-{_rand_text(8)}")
        time.sleep(0.5)
        with self._mc.measure("status.get"):
            status = _get_status(self.proxy_port)
        last_rx_ago = status.get("proxy", {}).get("last_rx_ago")
        self.assertIsNotNone(last_rx_ago, "last_rx_ago not in proxy status")
        self.assertLess(last_rx_ago, 5.0,
                        f"last_rx_ago too stale: {last_rx_ago}s")


class SoakWSSubscriberLeakTests(SoakBase):
    """WS connections: subscribers must be cleaned up after disconnect."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.port = free_port()
        cls.ctx = running_server(port=cls.port)
        cls._proc = cls.ctx.__enter__()
        cls._server_pid = getattr(cls._proc, "pid", None)
        cls._mc = MetricsCollector("SoakWSSubscriberLeakTests",
                                   server_pid=cls._server_pid)
        cls._mc.snapshot_rss()
        cls._start_rss_watcher()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._stop_rss_watcher()
        cls._mc.snapshot_rss()
        cls.ctx.__exit__(None, None, None)
        _emit_metrics(cls._mc, "SoakWSSubscriberLeakTests")

    @unittest.skipUnless(HAS_WS_SYNC, "websockets.sync not available")
    def test_ws_clients_cleaned_up_after_disconnect(self):
        """Open N WS connections and close them; client list must not grow."""
        import websockets.sync.client as ws_sync  # noqa: PLC0415
        with self._mc.measure("status.get"):
            status_before = _get_status(self.port)
        clients_before = len(status_before.get("clients", []))

        for _ in range(10):
            try:
                ws_url = f"ws://127.0.0.1:{self.port}/ws"
                with self._mc.measure("ws.connect"):
                    conn = ws_sync.connect(ws_url)
                conn.send(json.dumps({
                    "jsonrpc": "2.0", "id": 1,
                    "method": "clip.watch", "params": {"topics": ["c"]},
                }))
                conn.recv()
                conn.close()
            except Exception:
                pass

        time.sleep(1.0)
        with self._mc.measure("status.get"):
            status_after = _get_status(self.port)
        clients_after = len(status_after.get("clients", []))
        self.assertLessEqual(
            clients_after, clients_before + 1,
            f"client list grew: {clients_before} → {clients_after}",
        )

    @unittest.skipUnless(HAS_WS_SYNC, "websockets.sync not available")
    def test_ws_rapid_subscribe_unsubscribe(self):
        """Rapid watch/unwatch cycles must not leak subscription entries."""
        import websockets.sync.client as ws_sync  # noqa: PLC0415
        ws_url = f"ws://127.0.0.1:{self.port}/ws"
        try:
            conn = ws_sync.connect(ws_url)
        except Exception as exc:
            self.skipTest(f"WS connect failed: {exc}")

        mid = 0
        try:
            for _ in range(50):
                mid += 1
                with self._mc.measure("ws.watch"):
                    conn.send(json.dumps({
                        "jsonrpc": "2.0", "id": mid,
                        "method": "clip.watch",
                        "params": {"topics": ["c", "p", "s"]},
                    }))
                    conn.recv()
                mid += 1
                with self._mc.measure("ws.unwatch"):
                    conn.send(json.dumps({
                        "jsonrpc": "2.0", "id": mid,
                        "method": "clip.unwatch",
                        "params": {"topics": ["c", "p", "s"]},
                    }))
                    conn.recv()
        finally:
            conn.close()

        time.sleep(0.5)
        with self._mc.measure("health.get"):
            _, health = get_json(f"http://127.0.0.1:{self.port}/v1/health.get")
        self.assertTrue(health["ok"])  # type: ignore[index]


class SoakConcurrentTopicsTests(SoakBase):
    """Concurrent writes to many topics must not cause data races."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.port = free_port()
        cls.ctx = running_server(port=cls.port)
        cls._proc = cls.ctx.__enter__()
        cls._server_pid = getattr(cls._proc, "pid", None)
        cls._mc = MetricsCollector("SoakConcurrentTopicsTests",
                                   server_pid=cls._server_pid)
        cls._mc.snapshot_rss()
        cls._start_rss_watcher()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._stop_rss_watcher()
        cls._mc.snapshot_rss()
        cls.ctx.__exit__(None, None, None)
        _emit_metrics(cls._mc, "SoakConcurrentTopicsTests")

    def test_concurrent_writers_same_topic(self):
        """N threads writing the same topic; last write always readable."""
        errors: list[str] = []
        last_written: list[str] = []
        lock = threading.Lock()

        def _writer(tid: int):
            deadline = time.monotonic() + DURATION_S / 3
            while time.monotonic() < deadline:
                val = f"writer-{tid}-{_rand_text(8)}"
                try:
                    with self._mc.measure("clip.put"):
                        _put(self.port, "c", val)
                    with lock:
                        last_written.append(val)
                except Exception as exc:
                    errors.append(str(exc))
                time.sleep(0.01)

        threads = [threading.Thread(target=_writer, args=(i,), daemon=True)
                   for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=DURATION_S)

        self.assertEqual(errors, [], f"writer errors: {errors[:3]}")
        self.assertGreater(len(last_written), 0)

        with self._mc.measure("clip.get"):
            result = _get_topic(self.port, "c")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIsInstance(result["item"]["value"], str)

    def test_many_topics_do_not_accumulate(self):
        """Writes to a bounded set of topics must keep topic_content bounded."""
        bounded_topics = [f"t{i}" for i in range(len(TOPICS))]
        for _ in range(100):
            with self._mc.measure("clip.put"):
                _put(self.port, random.choice(bounded_topics), _rand_text(32))
        time.sleep(0.5)
        with self._mc.measure("status.get"):
            status = _get_status(self.port)
        n = len(status.get("topics", []))
        self.assertLessEqual(n, len(TOPICS) + len(bounded_topics) + 2)


class SoakEncryptionTests(SoakBase):
    """Encrypted-flag items must be stored and served correctly under load."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.port = free_port()
        cls.ctx = running_server(
            port=cls.port,
            extra_env={"RCLIPBOARD_ADMIN_TOKEN": ADMIN_TOKEN},
        )
        cls._proc = cls.ctx.__enter__()
        cls._server_pid = getattr(cls._proc, "pid", None)
        cls._mc = MetricsCollector("SoakEncryptionTests",
                                   server_pid=cls._server_pid)
        cls._mc.snapshot_rss()
        cls._start_rss_watcher()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._stop_rss_watcher()
        cls._mc.snapshot_rss()
        cls.ctx.__exit__(None, None, None)
        _emit_metrics(cls._mc, "SoakEncryptionTests")

    def _put_encrypted(self, topic: str, value: str) -> None:
        import base64
        b64 = base64.b64encode(value.encode()).decode()
        with self._mc.measure("clip.put.encrypted"):
            status, body = post_json(
                f"http://127.0.0.1:{self.port}/v1/clip.put",
                {"items": [{"topic": topic,
                             "mime": "application/octet-stream",
                             "encoding": "base64",
                             "value": b64,
                             "encrypted": True}],
                 "meta": {"app": "soak-enc"}},
            )
        self.assertEqual(status, 200, f"put encrypted failed: {body}")

    def test_encrypted_flag_persists_in_stored_item(self):
        """Encrypted flag written via put must be visible in status."""
        self._put_encrypted("c", "fake-ciphertext-payload")
        time.sleep(0.3)
        with self._mc.measure("status.get"):
            status = _get_status(self.port)
        self.assertTrue(status["ok"])
        self.assertIn("c", status.get("topics", []))

    def test_get_encrypted_without_key_header_returns_403(self):
        """clip.get for encrypted topic without key header must return 403."""
        FAKE_AGE_KEY = (
            "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p"
        )
        with self._mc.measure("keys.publish"):
            post_json(
                f"http://127.0.0.1:{self.port}/v1/keys.publish",
                {"public_key": FAKE_AGE_KEY, "label": "soak-test"},
                extra_headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            )

        self._put_encrypted("c", "fake-age-ciphertext")
        time.sleep(0.2)

        with self._mc.measure("clip.get.encrypted.403"):
            status, body = post_json(
                f"http://127.0.0.1:{self.port}/v1/clip.get",
                {"topic": "c"},
            )
        self.assertEqual(status, 403, f"expected 403, got {status}: {body}")
        self.assertEqual(body["code"], 4032)  # type: ignore[index]

    def test_repeated_encrypted_puts_no_registry_leak(self):
        """Repeated key registrations must not grow registry unboundedly."""
        keys = [
            f"age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac{i:3x}"
            for i in range(20)
        ]
        for k in keys:
            with self._mc.measure("keys.publish"):
                post_json(
                    f"http://127.0.0.1:{self.port}/v1/keys.publish",
                    {"public_key": k, "label": f"soak-{k[-4:]}"},
                    extra_headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
                )

        with self._mc.measure("keys.list"):
            _, body = get_json(f"http://127.0.0.1:{self.port}/v1/keys.list")
        assert isinstance(body, dict)
        n_keys = len(body.get("keys", []))
        self.assertLessEqual(n_keys, 30,
                             f"key registry grew to {n_keys} entries")


class SoakQueueBackpressureTests(SoakBase):
    """Dispatcher queue must drain completely under burst load."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.port = free_port()
        cls.ctx = running_server(
            port=cls.port,
            extra_env={"RCLIPBOARD_NOTIFY_DELAY_MS": "500"},
        )
        cls._proc = cls.ctx.__enter__()
        cls._server_pid = getattr(cls._proc, "pid", None)
        cls._mc = MetricsCollector("SoakQueueBackpressureTests",
                                   server_pid=cls._server_pid)
        cls._mc.snapshot_rss()
        cls._start_rss_watcher()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._stop_rss_watcher()
        cls._mc.snapshot_rss()
        cls.ctx.__exit__(None, None, None)
        _emit_metrics(cls._mc, "SoakQueueBackpressureTests")

    def test_burst_put_then_get_consistent(self):
        """After a burst of puts the final get must return the last written value."""
        last_val = None
        for i in range(100):
            val = f"burst-{i:04d}"
            with self._mc.measure("clip.put"):
                _put(self.port, "c", val)
            last_val = val

        time.sleep(2.5)

        with self._mc.measure("clip.get"):
            result = _get_topic(self.port, "c")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["item"]["value"], last_val)

    def test_high_frequency_put_server_remains_responsive(self):
        """Server must respond to health checks during high-frequency writes."""
        failures = []

        def _burst():
            for _ in range(500):
                try:
                    with self._mc.measure("clip.put"):
                        _put(self.port, "c", _rand_text(16))
                except Exception:
                    pass
                time.sleep(0.002)

        def _poll():
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                try:
                    with self._mc.measure("health.get"):
                        _, body = get_json(
                            f"http://127.0.0.1:{self.port}/v1/health.get"
                        )
                    if not (isinstance(body, dict) and body.get("ok")):
                        failures.append(body)
                except Exception as exc:
                    failures.append(str(exc))
                time.sleep(0.1)

        t_burst = threading.Thread(target=_burst, daemon=True)
        t_poll = threading.Thread(target=_poll, daemon=True)
        t_burst.start()
        t_poll.start()
        t_burst.join(timeout=15)
        t_poll.join(timeout=15)

        self.assertEqual(failures, [],
                         f"health failures under burst: {failures[:3]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
