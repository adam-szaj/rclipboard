from __future__ import annotations

import asyncio
import unittest

import websockets

from tests.helpers import free_port, running_server


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


if __name__ == "__main__":
    unittest.main()
