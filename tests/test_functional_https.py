from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import (
    free_port,
    gen_self_signed_cert,
    get_json_ssl,
    post_json_ssl,
    running_ssl_server,
    ssl_client_ctx,
)


class SSLHTTPSFunctionalTests(unittest.TestCase):
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

    def test_health_over_https(self):
        status, body = get_json_ssl(
            f"https://127.0.0.1:{self.port}/v1/health.get", self.ssl_ctx
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn("xsel_enabled", body)
        self.assertIn("pasteboard_enabled", body)

    def test_status_over_https(self):
        status, body = get_json_ssl(
            f"https://127.0.0.1:{self.port}/v1/status.get", self.ssl_ctx
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn("pasteboard", body)

    def test_clip_put_get_and_topics_list_over_https(self):
        payload = {
            "items": [
                {
                    "topic": "c",
                    "mime": "text/plain",
                    "encoding": "utf-8",
                    "value": "hello-https",
                }
            ],
            "meta": {"app": "ssl-test"},
        }
        status, body = post_json_ssl(
            f"https://127.0.0.1:{self.port}/v1/clip.put", payload, self.ssl_ctx
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["items"][0]["value"], "hello-https")

        status, body = post_json_ssl(
            f"https://127.0.0.1:{self.port}/v1/clip.get",
            {"topic": "c"},
            self.ssl_ctx,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["item"]["value"], "hello-https")
        self.assertEqual(body["item"]["encoding"], "utf-8")

        status, body = post_json_ssl(
            f"https://127.0.0.1:{self.port}/v1/topics.list", {}, self.ssl_ctx
        )
        self.assertEqual(status, 200)
        self.assertIn("c", body["topics"])

    def test_clip_get_not_found_returns_rpc_error_shape_over_https(self):
        status, body = post_json_ssl(
            f"https://127.0.0.1:{self.port}/v1/clip.get",
            {"topic": "missing-ssl"},
            self.ssl_ctx,
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], 1001)
        self.assertEqual(body["message"], "Topic not found")
        self.assertEqual(body["data"]["topic"], "missing-ssl")


if __name__ == "__main__":
    unittest.main()
