from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import time

from tests.helpers import (
    free_port,
    gen_self_signed_cert,
    post_json,
    post_json_ssl,
    running_server,
    running_ssl_server,
    ssl_client_ctx,
    wait_for_value,
)


class ProxySSLUpstreamIntegrationTests(unittest.TestCase):
    """Proxy (plain HTTP) connects to an upstream server over WSS.

    The upstream runs HTTPS/WSS; the proxy receives ``RCLIPBOARD_SSL_CA_BUNDLE``
    so it can verify the upstream's self-signed certificate.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmpdir.name)
        cls.cert, cls.key = gen_self_signed_cert(tmp)
        cls.ssl_ctx = ssl_client_ctx(cls.cert)

        cls.upstream_port = free_port()
        cls.proxy_port = free_port()

        cls.upstream_ctx = running_ssl_server(
            port=cls.upstream_port,
            certfile=cls.cert,
            keyfile=cls.key,
        )
        cls.upstream_ctx.__enter__()

        cls.proxy_ctx = running_server(
            port=cls.proxy_port,
            proxy=True,
            upstream_port=cls.upstream_port,
            extra_env={
                "RCLIPBOARD_UPSTREAM_ENDPOINT": f"https://127.0.0.1:{cls.upstream_port}",
                "RCLIPBOARD_SSL_CA_BUNDLE": str(cls.cert),
            },
        )
        cls.proxy_ctx.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.proxy_ctx.__exit__(None, None, None)
        cls.upstream_ctx.__exit__(None, None, None)
        cls._tmpdir.cleanup()

    def test_upstream_update_reaches_proxy_cache(self):
        status, body = post_json_ssl(
            f"https://127.0.0.1:{self.upstream_port}/v1/clip.put",
            {
                "items": [
                    {
                        "topic": "c",
                        "mime": "text/plain",
                        "encoding": "utf-8",
                        "value": "ssl-from-upstream",
                    }
                ],
                "meta": {"app": "ssl-proxy-test"},
            },
            self.ssl_ctx,
        )
        self.assertEqual(status, 200)
        cached = wait_for_value(self.proxy_port, "c", "ssl-from-upstream")
        self.assertEqual(cached["item"]["value"], "ssl-from-upstream")

    def test_proxy_local_update_is_forwarded_to_ssl_upstream(self):
        status, body = post_json(
            f"http://127.0.0.1:{self.proxy_port}/v1/clip.put",
            {
                "items": [
                    {
                        "topic": "p",
                        "mime": "text/plain",
                        "encoding": "utf-8",
                        "value": "ssl-from-proxy",
                    }
                ],
                "meta": {"app": "ssl-proxy-test"},
            },
        )
        self.assertEqual(status, 200)

        deadline = time.monotonic() + 10.0
        last_body = None
        while time.monotonic() < deadline:
            s, b = post_json_ssl(
                f"https://127.0.0.1:{self.upstream_port}/v1/clip.get",
                {"topic": "p"},
                self.ssl_ctx,
            )
            last_body = b
            if s == 200 and isinstance(b, dict):
                item = b.get("item")
                if isinstance(item, dict) and item.get("value") == "ssl-from-proxy":
                    break
            time.sleep(0.1)
        else:
            raise AssertionError(
                f"timed out waiting for topic=p on SSL upstream, last={last_body!r}"
            )
        self.assertEqual(last_body["item"]["value"], "ssl-from-proxy")


class ProxySSLBothSidesIntegrationTests(unittest.TestCase):
    """Both proxy and upstream run HTTPS/WSS."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmpdir.name)
        cls.cert, cls.key = gen_self_signed_cert(tmp)
        cls.ssl_ctx = ssl_client_ctx(cls.cert)

        cls.upstream_port = free_port()
        cls.proxy_port = free_port()

        cls.upstream_ctx = running_ssl_server(
            port=cls.upstream_port,
            certfile=cls.cert,
            keyfile=cls.key,
        )
        cls.upstream_ctx.__enter__()

        cls.proxy_ctx = running_ssl_server(
            port=cls.proxy_port,
            certfile=cls.cert,
            keyfile=cls.key,
            proxy=True,
            upstream_port=cls.upstream_port,
            extra_env={
                "RCLIPBOARD_UPSTREAM_ENDPOINT": f"https://127.0.0.1:{cls.upstream_port}",
                "RCLIPBOARD_SSL_CA_BUNDLE": str(cls.cert),
            },
        )
        cls.proxy_ctx.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.proxy_ctx.__exit__(None, None, None)
        cls.upstream_ctx.__exit__(None, None, None)
        cls._tmpdir.cleanup()

    def test_upstream_update_reaches_https_proxy_cache(self):
        status, body = post_json_ssl(
            f"https://127.0.0.1:{self.upstream_port}/v1/clip.put",
            {
                "items": [
                    {
                        "topic": "s",
                        "mime": "text/plain",
                        "encoding": "utf-8",
                        "value": "full-ssl-upstream",
                    }
                ],
                "meta": {"app": "full-ssl-test"},
            },
            self.ssl_ctx,
        )
        self.assertEqual(status, 200)

        import time
        deadline = time.monotonic() + 10.0
        last_body = None
        while time.monotonic() < deadline:
            s, b = post_json_ssl(
                f"https://127.0.0.1:{self.proxy_port}/v1/clip.get",
                {"topic": "s"},
                self.ssl_ctx,
            )
            last_body = b
            if s == 200 and isinstance(b, dict):
                item = b.get("item")
                if isinstance(item, dict) and item.get("value") == "full-ssl-upstream":
                    break
            time.sleep(0.1)
        else:
            raise AssertionError(
                f"timed out waiting for topic=s on SSL proxy, last={last_body!r}"
            )
        self.assertEqual(last_body["item"]["value"], "full-ssl-upstream")


if __name__ == "__main__":
    unittest.main()
