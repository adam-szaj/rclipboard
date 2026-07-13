"""Functional tests for the monitoring surface.

Pin down the observable monitor behavior (snapshot shape, client
classification, topic counters, stream events) so the monitoring state can
be refactored (MonitorHub extraction) without silent drift.
"""
from __future__ import annotations

import asyncio
import json
import time
import unittest

from tests.helpers import (
    free_port,
    get_json,
    post_json,
    running_server,
)


def _put(port: int, topic: str, value: str, app_name: str = "montest"):
    return post_json(
        f"http://127.0.0.1:{port}/v1/clip.put",
        {
            "items": [
                {"topic": topic, "mime": "text/plain",
                 "encoding": "utf-8", "value": value}
            ],
            "meta": {"app": app_name},
        },
    )


def _snapshot(port: int) -> dict:
    status, body = get_json(f"http://127.0.0.1:{port}/v1/monitor.snapshot")
    assert status == 200, body
    assert isinstance(body, dict)
    return body


def _topic_entry(snap: dict, topic: str) -> dict | None:
    for entry in snap.get("topics", []):
        if entry.get("topic") == topic:
            return entry
    return None


class MonitorSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.port = free_port()
        self.ctx = running_server(port=self.port, proxy=False)
        self.ctx.__enter__()

    def tearDown(self) -> None:
        self.ctx.__exit__(None, None, None)

    def test_snapshot_shape_and_topic_counters(self):
        status, _ = _put(self.port, "c", "monitor-me")
        self.assertEqual(status, 200)
        # a clip.get bumps the topic's get counter
        status, _ = post_json(
            f"http://127.0.0.1:{self.port}/v1/clip.get", {"topic": "c"})
        self.assertEqual(status, 200)

        snap = _snapshot(self.port)
        self.assertIn("ts_utc", snap)
        self.assertIn("clients", snap)
        self.assertIn("topics", snap)
        self.assertIn("proxy", snap)

        entry = _topic_entry(snap, "c")
        self.assertIsNotNone(entry, f"topic 'c' missing in snapshot: {snap}")
        assert entry is not None
        self.assertEqual(entry["size"], len("monitor-me"))
        self.assertEqual(entry["source_app"], "montest")
        self.assertGreaterEqual(entry["get_count"], 1)

    def test_ws_client_is_classified_and_notified(self):
        import websockets.sync.client as ws_client

        uri = f"ws://127.0.0.1:{self.port}/ws"
        with ws_client.connect(uri) as ws:
            ws.send(json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "clip.watch",
                "params": {"topics": ["c"]},
            }))
            ws.recv(timeout=5)  # watch reply

            # a registered WS connection must show up as kind "ws"
            snap = _snapshot(self.port)
            ws_clients = [c for c in snap["clients"] if c["kind"] == "ws"]
            self.assertTrue(ws_clients, f"no ws client in {snap['clients']}")
            conn_id = ws_clients[0]["conn_id"]
            self.assertTrue(conn_id.startswith("ws:"), conn_id)

            # a put on the watched topic must (after the notify debounce)
            # bump the notify counter and record the notified client
            status, _ = _put(self.port, "c", "notify-me")
            self.assertEqual(status, 200)
            ws.recv(timeout=5)  # the clip.changed notification itself

            deadline = time.monotonic() + 5.0
            entry = None
            while time.monotonic() < deadline:
                entry = _topic_entry(_snapshot(self.port), "c")
                if entry and entry["notify_count"] >= 1:
                    break
                time.sleep(0.1)
            assert entry is not None
            self.assertGreaterEqual(entry["notify_count"], 1)
            self.assertIn(conn_id, entry["notified_clients_ago"])


class MonitorStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.port = free_port()
        self.ctx = running_server(port=self.port, proxy=False)
        self.ctx.__enter__()

    def tearDown(self) -> None:
        self.ctx.__exit__(None, None, None)

    def test_stream_snapshot_then_topic_put_event(self):
        from websockets.asyncio.client import connect

        async def _run() -> dict:
            uri = f"ws://127.0.0.1:{self.port}/v1/monitor.stream"
            async with connect(uri) as ws:
                first = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                assert first["kind"] == "snapshot", first

                status, _ = await asyncio.to_thread(
                    _put, self.port, "c", "stream-me")
                assert status == 200

                # read frames until the topic.put event arrives
                deadline = asyncio.get_event_loop().time() + 5.0
                while True:
                    remaining = deadline - asyncio.get_event_loop().time()
                    frame = json.loads(
                        await asyncio.wait_for(ws.recv(), timeout=remaining))
                    if frame.get("kind") == "topic.put":
                        return frame

        frame = asyncio.run(_run())
        self.assertEqual(frame["topic"], "c")
        self.assertEqual(frame["data"].get("size"), len("stream-me"))
        self.assertEqual(frame["data"].get("app"), "montest")


if __name__ == "__main__":
    unittest.main()
