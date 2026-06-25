from __future__ import annotations

import asyncio
import json
import time
import unittest

import websockets

from tests.helpers import (
    free_port,
    post_json,
    running_server,
    start_server,
    stop_process,
    wait_http_ready,
    wait_for_value,
)


class ProxyIntegrationTests(unittest.TestCase):
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
        )
        cls.proxy_ctx.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.proxy_ctx.__exit__(None, None, None)
        cls.upstream_ctx.__exit__(None, None, None)

    def test_upstream_update_reaches_proxy_cache(self):
        status, body = post_json(
            f"http://127.0.0.1:{self.upstream_port}/v1/clip.put",
            {
                "items": [
                    {
                        "topic": "c",
                        "mime": "text/plain",
                        "encoding": "utf-8",
                        "value": "from-upstream",
                    }
                ],
                "meta": {"app": "upstream-test"},
            },
        )
        self.assertEqual(status, 200)
        cached = wait_for_value(self.proxy_port, "c", "from-upstream")
        self.assertEqual(cached["item"]["value"], "from-upstream")

    def test_proxy_local_update_is_forwarded_upstream(self):
        status, body = post_json(
            f"http://127.0.0.1:{self.proxy_port}/v1/clip.put",
            {
                "items": [
                    {
                        "topic": "p",
                        "mime": "text/plain",
                        "encoding": "utf-8",
                        "value": "from-proxy",
                    }
                ],
                "meta": {"app": "proxy-test"},
            },
        )
        self.assertEqual(status, 200)
        upstream = wait_for_value(self.upstream_port, "p", "from-proxy")
        self.assertEqual(upstream["item"]["value"], "from-proxy")

    def test_proxy_forward_stamps_via_and_host_meta(self):
        # clip.get does not expose meta, so observe the forwarded clip.changed on
        # the upstream via a WebSocket watcher to inspect the meta the proxy sent.
        async def run() -> dict:
            uri = f"ws://127.0.0.1:{self.upstream_port}/ws"
            async with websockets.connect(uri) as ws:
                await ws.send(
                    '{"jsonrpc":"2.0","id":"w","method":"clip.watch","params":{"topics":["s"]}}'
                )
                await asyncio.wait_for(ws.recv(), timeout=5)
                # Put on the PROXY; it forwards upstream as clip.put.
                status, _ = post_json(
                    f"http://127.0.0.1:{self.proxy_port}/v1/clip.put",
                    {
                        "items": [
                            {"topic": "s", "mime": "text/plain",
                             "encoding": "utf-8", "value": "stamp-me"}
                        ],
                        "meta": {"app": "cli"},
                    },
                )
                assert status == 200
                for _ in range(5):
                    frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                    if frame.get("method") == "clip.changed":
                        return frame["params"].get("meta", {})
            return {}

        meta = asyncio.run(run())
        self.assertEqual(meta.get("via"), "proxy")
        self.assertTrue(meta.get("host"), "expected a non-empty host in meta")
        # original client meta is preserved
        self.assertEqual(meta.get("app"), "cli")


class ProxyShutdownTests(unittest.TestCase):
    def test_proxy_stops_promptly_with_live_upstream(self):
        # A proxy holds an open upstream WebSocket. On SIGTERM the proxy must
        # broadcast server.shutdown (to the upstream, via ProxyClient) and exit
        # promptly — the upstream link must not turn shutdown into a hang.
        upstream_port = free_port()
        proxy_port = free_port()
        with running_server(port=upstream_port, proxy=False):
            proxy = start_server(
                port=proxy_port, proxy=True, upstream_port=upstream_port
            )
            try:
                wait_http_ready(proxy_port)
                # Let the proxy establish its upstream connection.
                time.sleep(0.5)
                t0 = time.monotonic()
                proxy.terminate()
                proxy.wait(timeout=10)
                elapsed = time.monotonic() - t0
                self.assertLess(elapsed, 5.0, f"proxy shutdown took {elapsed:.2f}s")
            finally:
                stop_process(proxy)


if __name__ == "__main__":
    unittest.main()
