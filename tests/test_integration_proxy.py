from __future__ import annotations

import unittest

from tests.helpers import (
    free_port,
    post_json,
    running_server,
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


if __name__ == "__main__":
    unittest.main()
