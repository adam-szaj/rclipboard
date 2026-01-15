from __future__ import annotations

import unittest

from tests.helpers import free_port, post_json, get_json, running_server


class FunctionalHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.port = free_port()
        self.server_ctx = running_server(port=self.port)
        self.server_ctx.__enter__()

    def tearDown(self) -> None:
        self.server_ctx.__exit__(None, None, None)

    def test_health_status_and_topics(self):
        status, body = get_json(f"http://127.0.0.1:{self.port}/v1/health.get")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn("xsel_enabled", body)
        self.assertIn("xsel_good", body)

        status, body = get_json(f"http://127.0.0.1:{self.port}/v1/status.get")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn("xsel", body)

        status, body = post_json(f"http://127.0.0.1:{self.port}/v1/topics.list", {})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"topics": []})

    def test_clip_put_get_and_topics_list(self):
        payload = {
            "items": [
                {
                    "topic": "c",
                    "mime": "text/plain",
                    "encoding": "utf-8",
                    "value": "hello-http",
                }
            ],
            "meta": {"app": "unittest"},
        }
        status, body = post_json(f"http://127.0.0.1:{self.port}/v1/clip.put", payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["items"][0]["value"], "hello-http")

        status, body = post_json(
            f"http://127.0.0.1:{self.port}/v1/clip.get",
            {"topic": "c"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["item"]["value"], "hello-http")
        self.assertEqual(body["item"]["encoding"], "utf-8")

        status, body = post_json(f"http://127.0.0.1:{self.port}/v1/topics.list", {})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"topics": ["c"]})

    def test_clip_get_not_found_returns_rpc_error_shape(self):
        status, body = post_json(
            f"http://127.0.0.1:{self.port}/v1/clip.get",
            {"topic": "missing"},
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], 1001)
        self.assertEqual(body["message"], "Topic not found")
        self.assertEqual(body["data"]["topic"], "missing")


if __name__ == "__main__":
    unittest.main()
