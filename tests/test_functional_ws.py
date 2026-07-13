from __future__ import annotations

import asyncio
import json
import time
import unittest

import websockets

from tests.helpers import free_port, running_server, start_server, stop_process, wait_http_ready


class FunctionalWebSocketTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.port = free_port()
        self.server_ctx = running_server(port=self.port)
        self.server_ctx.__enter__()

    def tearDown(self) -> None:
        self.server_ctx.__exit__(None, None, None)

    async def test_watch_put_get_and_changed_event(self):
        watch_uri = f"ws://127.0.0.1:{self.port}/ws"
        put_uri = f"ws://127.0.0.1:{self.port}/ws"

        async with websockets.connect(watch_uri) as watcher:
            await watcher.send(
                """
                {"jsonrpc":"2.0","id":"watch-1","method":"clip.watch","params":{"topics":["c"]}}
                """.strip()
            )
            reply = await asyncio.wait_for(watcher.recv(), timeout=5)
            self.assertIn('"id":"watch-1"', reply.replace(" ", ""))
            self.assertIn('"topics":["c"]', reply.replace(" ", ""))

            async with websockets.connect(put_uri) as writer:
                await writer.send(
                    """
                    {"jsonrpc":"2.0","id":"put-1","method":"clip.put","params":{"items":[{"topic":"c","mime":"text/plain","encoding":"utf-8","value":"hello-ws"}],"meta":{"app":"ws-test"}}}
                    """.strip()
                )
                put_reply = await asyncio.wait_for(writer.recv(), timeout=5)
                self.assertIn('"id":"put-1"', put_reply.replace(" ", ""))
                self.assertIn('"hello-ws"', put_reply)

                await writer.send(
                    """
                    {"jsonrpc":"2.0","id":"get-1","method":"clip.get","params":{"topic":"c"}}
                    """.strip()
                )
                get_reply = await asyncio.wait_for(writer.recv(), timeout=5)
                self.assertIn('"id":"get-1"', get_reply.replace(" ", ""))
                self.assertIn('"hello-ws"', get_reply)

            event = await asyncio.wait_for(watcher.recv(), timeout=5)
            compact = event.replace(" ", "")
            self.assertIn('"method":"clip.changed"', compact)
            self.assertIn('"value":"hello-ws"', compact)

    async def test_rewatch_returns_contents_for_already_watched_topics(self):
        """A repeated clip.watch on the same connection must return the
        current contents for already-watched topics, not an empty dict."""
        uri = f"ws://127.0.0.1:{self.port}/ws"
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({
                "jsonrpc": "2.0", "id": "put-1", "method": "clip.put",
                "params": {"items": [{"topic": "c", "mime": "text/plain",
                                      "encoding": "utf-8", "value": "rewatch-me"}],
                           "meta": {}},
            }))
            await asyncio.wait_for(ws.recv(), timeout=5)

            for watch_id in ("watch-1", "watch-2"):
                await ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": watch_id, "method": "clip.watch",
                    "params": {"topics": ["c"]},
                }))
                reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                contents = reply["result"].get("contents", {})
                self.assertIn("c", contents,
                              f"{watch_id}: watch reply must carry current contents")
                self.assertEqual(contents["c"]["value"]["value"], "rewatch-me")

    async def test_invalid_params_return_jsonrpc_error(self):
        uri = f"ws://127.0.0.1:{self.port}/ws"
        async with websockets.connect(uri) as ws:
            await ws.send(
                """
                {"jsonrpc":"2.0","id":"bad-1","method":"clip.get","params":{"wrong":"shape"}}
                """.strip()
            )
            reply = await asyncio.wait_for(ws.recv(), timeout=5)
            compact = reply.replace(" ", "")
            self.assertIn('"id":"bad-1"', compact)
            self.assertIn('"error"', compact)
            self.assertIn('"code":1000', compact)

    async def test_clip_get_flushes_throttled_notification(self):
        self.server_ctx.__exit__(None, None, None)
        self.port = free_port()
        self.server_ctx = running_server(
            port=self.port,
            extra_env={"RCLIPBOARD_NOTIFY_DELAY_MS": "5000"},
        )
        self.server_ctx.__enter__()

        uri = f"ws://127.0.0.1:{self.port}/ws"
        async with websockets.connect(uri) as watcher:
            await watcher.send(
                """
                {"jsonrpc":"2.0","id":"watch-throttle","method":"clip.watch","params":{"topics":["c"]}}
                """.strip()
            )
            await asyncio.wait_for(watcher.recv(), timeout=5)

            async with websockets.connect(uri) as writer:
                await writer.send(
                    """
                    {"jsonrpc":"2.0","id":"put-throttle","method":"clip.put","params":{"items":[{"topic":"c","mime":"text/plain","encoding":"utf-8","value":"hello-throttle"}],"meta":{"app":"ws-test"}}}
                    """.strip()
                )
                await asyncio.wait_for(writer.recv(), timeout=5)

                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(watcher.recv(), timeout=0.2)

                await writer.send(
                    """
                    {"jsonrpc":"2.0","id":"get-throttle","method":"clip.get","params":{"topic":"c"}}
                    """.strip()
                )
                get_reply = await asyncio.wait_for(writer.recv(), timeout=5)
                self.assertIn('"id":"get-throttle"', get_reply.replace(" ", ""))
                self.assertIn('"hello-throttle"', get_reply)

            event = await asyncio.wait_for(watcher.recv(), timeout=2)
            compact = event.replace(" ", "")
            self.assertIn('"method":"clip.changed"', compact)
            self.assertIn('"value":"hello-throttle"', compact)


class ShutdownNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_notice_and_fast_exit(self):
        # Manage the server by hand so we can SIGTERM it and time the exit.
        port = free_port()
        proc = start_server(port=port)
        try:
            wait_http_ready(port)
            uri = f"ws://127.0.0.1:{port}/ws"
            monitor_uri = f"ws://127.0.0.1:{port}/v1/monitor.stream"

            async with websockets.connect(uri) as watcher, \
                    websockets.connect(monitor_uri) as monitor:
                # WS watcher: subscribe so the connection is a live RPC client.
                await watcher.send(
                    '{"jsonrpc":"2.0","id":"w","method":"clip.watch","params":{"topics":["c"]}}'
                )
                await asyncio.wait_for(watcher.recv(), timeout=5)

                # Monitor: drain the initial snapshot frame.
                snap = json.loads(await asyncio.wait_for(monitor.recv(), timeout=5))
                self.assertEqual(snap.get("kind"), "snapshot")

                # Trigger shutdown (SIGTERM) and time how long the process takes to exit.
                t0 = time.monotonic()
                proc.terminate()

                # WS watcher receives the server.shutdown notification.
                notice = json.loads(await asyncio.wait_for(watcher.recv(), timeout=5))
                self.assertEqual(notice.get("method"), "server.shutdown")
                self.assertIn("reason", notice.get("params", {}))
                self.assertIn("ts_utc", notice.get("params", {}))

                # Then the connection is actively closed by the server.
                with self.assertRaises(websockets.exceptions.ConnectionClosed):
                    await asyncio.wait_for(watcher.recv(), timeout=5)

                # Monitor receives the service.stop event, then closes.
                got_service_stop = False
                with self.assertRaises(websockets.exceptions.ConnectionClosed):
                    while True:
                        frame = json.loads(await asyncio.wait_for(monitor.recv(), timeout=5))
                        if frame.get("kind") == "service.stop":
                            got_service_stop = True
                self.assertTrue(got_service_stop, "monitor never received service.stop")

            # Process must exit promptly because connections self-closed — far
            # below any graceful-shutdown timeout. (Harness runs uvicorn with the
            # default infinite graceful timeout, so a hang here would block ~forever.)
            proc.wait(timeout=10)
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 5.0, f"shutdown took {elapsed:.2f}s — too slow")
        finally:
            stop_process(proc)


if __name__ == "__main__":
    unittest.main()
