"""Integration tests for time-based sync between proxy and upstream.

Strategy under test: "newer wins; on a tie the local value wins". Timestamps
are supplied explicitly via meta["ts"] so the outcome is deterministic and
independent of the (identical, same-host) wall clocks in the test harness.
"""
from __future__ import annotations

import time
import unittest

from tests.helpers import (
    free_port,
    get_json,
    post_json,
    running_server,
    wait_for_value,
)

OLD_TS = "2020-01-01T00:00:00+00:00"
NEW_TS = "2030-01-01T00:00:00+00:00"


def _put(port: int, topic: str, value: str, ts: str):
    return post_json(
        f"http://127.0.0.1:{port}/v1/clip.put",
        {
            "items": [
                {"topic": topic, "mime": "text/plain",
                 "encoding": "utf-8", "value": value}
            ],
            "meta": {"ts": ts},
        },
    )


def _get(port: int, topic: str):
    # GET /v1/clip/{topic} returns a TopicData: {"topic","value":{"value",..},..}
    status, body = get_json(f"http://127.0.0.1:{port}/v1/clip/{topic}")
    return status, body


def _value(body) -> str:
    return body["value"]["value"]


class ProxySyncConflictTests(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream_port = free_port()
        self.proxy_port = free_port()
        self.upstream_ctx = running_server(port=self.upstream_port, proxy=False)
        self.upstream_ctx.__enter__()
        self.proxy_ctx = running_server(
            port=self.proxy_port, proxy=True, upstream_port=self.upstream_port)
        self.proxy_ctx.__enter__()

    def tearDown(self) -> None:
        self.proxy_ctx.__exit__(None, None, None)
        self.upstream_ctx.__exit__(None, None, None)

    def test_newer_upstream_value_replaces_older_local(self):
        # Older value already on the proxy; a newer value arrives from upstream.
        status, _ = _put(self.proxy_port, "c", "old-local", OLD_TS)
        self.assertEqual(status, 200)
        status, _ = _put(self.upstream_port, "c", "new-upstream", NEW_TS)
        self.assertEqual(status, 200)
        cached = wait_for_value(self.proxy_port, "c", "new-upstream")
        self.assertEqual(cached["item"]["value"], "new-upstream")

    def test_older_upstream_value_does_not_replace_newer_local(self):
        # Newer value on the proxy must survive an older value from upstream.
        status, _ = _put(self.proxy_port, "p", "new-local", NEW_TS)
        self.assertEqual(status, 200)
        # let the proxy forward its value upstream first
        wait_for_value(self.upstream_port, "p", "new-local")

        # now upstream receives an OLDER value for the same topic
        status, _ = _put(self.upstream_port, "p", "old-upstream", OLD_TS)
        self.assertEqual(status, 200)

        # upstream itself must reject the older value (newer wins centrally)
        time.sleep(1.0)
        st, body = _get(self.upstream_port, "p")
        self.assertEqual(st, 200)
        self.assertEqual(
            _value(body), "new-local",
            "upstream must keep the newer value, not the older put",
        )

        # and the proxy must still hold its newer local value
        st, body = _get(self.proxy_port, "p")
        self.assertEqual(st, 200)
        self.assertEqual(_value(body), "new-local")

    def test_tie_local_wins(self):
        # Same timestamp on both sides → the local (proxy) value must win.
        status, _ = _put(self.upstream_port, "s", "upstream-tie", NEW_TS)
        self.assertEqual(status, 200)
        # ensure proxy has ingested upstream's value first
        wait_for_value(self.proxy_port, "s", "upstream-tie")

        # local put with the SAME ts — tie → local wins
        status, _ = _put(self.proxy_port, "s", "local-tie", NEW_TS)
        self.assertEqual(status, 200)

        time.sleep(1.0)
        st, body = _get(self.proxy_port, "s")
        self.assertEqual(st, 200)
        self.assertEqual(
            _value(body), "local-tie",
            "on a tie the local value must win",
        )


class WatchHandshakeClockTests(unittest.TestCase):
    """The clip.watch reply must carry server_now_utc for offset exchange."""

    def setUp(self) -> None:
        self.port = free_port()
        self.ctx = running_server(port=self.port, proxy=False)
        self.ctx.__enter__()

    def tearDown(self) -> None:
        self.ctx.__exit__(None, None, None)

    def test_watch_reply_includes_server_now_utc(self):
        import asyncio
        import json

        from websockets.asyncio.client import connect

        async def _run() -> dict:
            uri = f"ws://127.0.0.1:{self.port}/ws"
            async with connect(uri) as ws:
                await ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "clip.watch",
                    "params": {
                        "topics": ["c"],
                        "peer_now_utc": "2026-06-03T12:00:00+00:00",
                    },
                }))
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                return json.loads(raw)

        reply = asyncio.run(_run())
        self.assertIn("result", reply)
        self.assertIn("server_now_utc", reply["result"])
        self.assertTrue(reply["result"]["server_now_utc"])


class InitialSyncEncodingTests(unittest.TestCase):
    """The clip.watch initial-sync reply must preserve value type/encoding.

    Regression for the proxy double-base64 bug: a base64/binary item that
    already exists on the upstream *before* the proxy connects is replicated
    via the clip.watch reply, which ships a raw ValueData. If that ValueData
    round-trips through the default (field-name) JSON dump without
    populate_by_name, the type/encoding aliases are lost and the value is
    mislabeled text/plain, causing consumers to base64-encode it a second time.
    """

    def _get_full(self, port: int, topic: str):
        # /v1/clip/{topic} returns the internal TopicData, including
        # value.encoding / value.type, so we can assert they survived the sync.
        return get_json(f"http://127.0.0.1:{port}/v1/clip/{topic}")

    def test_base64_item_synced_before_connect_keeps_encoding(self):
        upstream_port = free_port()
        proxy_port = free_port()

        # base64("hi\x00\xff") — a binary value that MUST stay base64/binary.
        b64_value = "aGkA/w=="

        upstream_ctx = running_server(port=upstream_port, proxy=False)
        upstream_ctx.__enter__()
        try:
            # Put the item on the upstream BEFORE the proxy exists, so it is
            # replicated through the clip.watch initial-sync path (not
            # clip.changed).
            status, _ = post_json(
                f"http://127.0.0.1:{upstream_port}/v1/clip.put",
                {
                    "items": [
                        {"topic": "c", "mime": "application/octet-stream",
                         "encoding": "base64", "value": b64_value}
                    ],
                    "meta": {"ts": NEW_TS},
                },
            )
            self.assertEqual(status, 200)

            proxy_ctx = running_server(
                port=proxy_port, proxy=True, upstream_port=upstream_port)
            proxy_ctx.__enter__()
            try:
                # Wait until the proxy has ingested the initial-sync value.
                deadline = time.monotonic() + 10.0
                body = None
                while time.monotonic() < deadline:
                    status, body = self._get_full(proxy_port, "c")
                    if status == 200 and isinstance(body, dict):
                        break
                    time.sleep(0.1)
                self.assertEqual(status, 200, f"proxy never synced: {body!r}")
                assert isinstance(body, dict)

                value = body["value"]
                # The value string is stored exactly once (single base64) ...
                self.assertEqual(value["value"], b64_value)
                # ... and, crucially, the encoding/type labels survived the
                # clip.watch reply round-trip (were previously lost -> plain/text).
                self.assertEqual(value["encoding"], "base64")
                self.assertEqual(value["type"], "binary")
            finally:
                proxy_ctx.__exit__(None, None, None)
        finally:
            upstream_ctx.__exit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
