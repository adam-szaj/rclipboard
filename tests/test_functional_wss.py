from __future__ import annotations

import asyncio
import ssl
import tempfile
import unittest
from pathlib import Path

import websockets

from tests.helpers import (
    free_port,
    gen_self_signed_cert,
    running_ssl_server,
    ssl_client_ctx,
)


class SSLWSSFunctionalTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmpdir.name)
        cls.cert, cls.key = gen_self_signed_cert(tmp)
        cls.ssl_ctx = ssl_client_ctx(cls.cert)
        cls.port = free_port()
        cls.server_ctx = running_ssl_server(port=cls.port, certfile=cls.cert, keyfile=cls.key)
        cls.server_ctx.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server_ctx.__exit__(None, None, None)
        cls._tmpdir.cleanup()

    async def test_watch_put_get_and_changed_event_over_wss(self):
        uri = f"wss://127.0.0.1:{self.port}/ws"

        async with websockets.connect(uri, ssl=self.ssl_ctx) as watcher:
            await watcher.send(
                '{"jsonrpc":"2.0","id":"watch-ssl-1","method":"clip.watch","params":{"topics":["c"]}}'
            )
            reply = await asyncio.wait_for(watcher.recv(), timeout=5)
            self.assertIn('"id":"watch-ssl-1"', reply.replace(" ", ""))
            self.assertIn('"topics":["c"]', reply.replace(" ", ""))

            async with websockets.connect(uri, ssl=self.ssl_ctx) as writer:
                await writer.send(
                    '{"jsonrpc":"2.0","id":"put-ssl-1","method":"clip.put",'
                    '"params":{"items":[{"topic":"c","mime":"text/plain",'
                    '"encoding":"utf-8","value":"hello-wss"}],"meta":{"app":"wss-test"}}}'
                )
                put_reply = await asyncio.wait_for(writer.recv(), timeout=5)
                self.assertIn('"id":"put-ssl-1"', put_reply.replace(" ", ""))
                self.assertIn('"hello-wss"', put_reply)

                await writer.send(
                    '{"jsonrpc":"2.0","id":"get-ssl-1","method":"clip.get","params":{"topic":"c"}}'
                )
                get_reply = await asyncio.wait_for(writer.recv(), timeout=5)
                self.assertIn('"id":"get-ssl-1"', get_reply.replace(" ", ""))
                self.assertIn('"hello-wss"', get_reply)

            event = await asyncio.wait_for(watcher.recv(), timeout=5)
            compact = event.replace(" ", "")
            self.assertIn('"method":"clip.changed"', compact)
            self.assertIn('"value":"hello-wss"', compact)

    async def test_invalid_params_return_jsonrpc_error_over_wss(self):
        uri = f"wss://127.0.0.1:{self.port}/ws"
        async with websockets.connect(uri, ssl=self.ssl_ctx) as ws:
            await ws.send(
                '{"jsonrpc":"2.0","id":"bad-ssl-1","method":"clip.get","params":{"wrong":"shape"}}'
            )
            reply = await asyncio.wait_for(ws.recv(), timeout=5)
            compact = reply.replace(" ", "")
            self.assertIn('"id":"bad-ssl-1"', compact)
            self.assertIn('"error"', compact)
            self.assertIn('"code":1000', compact)

    async def test_plain_ws_rejected_by_ssl_server(self):
        """Connecting with plain ws:// to an SSL server must fail."""
        uri = f"ws://127.0.0.1:{self.port}/ws"
        with self.assertRaises(Exception):
            async with websockets.connect(uri) as ws:
                await ws.recv()


if __name__ == "__main__":
    unittest.main()
