from __future__ import annotations

import unittest

from tests.helpers import free_port, get_json, post_json, running_server

FAKE_KEY = "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p"
OTHER_KEY = "age1qj2c3y4p5r6s7t8u9v0w1x2y3z4a5b6c7d8e9f0g1h2i3j4k5l6m7n8o9p"
ADMIN_TOKEN = "test-admin-token"


class KeysRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.port = free_port()
        self.server_ctx = running_server(
            port=self.port,
            extra_env={"RCLIPBOARD_ADMIN_TOKEN": ADMIN_TOKEN},
        )
        self.server_ctx.__enter__()

    def tearDown(self) -> None:
        self.server_ctx.__exit__(None, None, None)

    def _base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # ── keys.list ────────────────────────────────────────────────────────────
    def _list_keys(self) -> tuple[int, object]:
        return get_json(
            f"{self._base()}/v1/keys.list",
            extra_headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )

    def test_keys_list_empty(self):
        status, body = self._list_keys()
        self.assertEqual(status, 200)
        self.assertEqual(body, {"keys": []})

    def test_keys_list_requires_admin_token(self):
        status, body = get_json(f"{self._base()}/v1/keys.list")
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], 4031)

    # ── keys.publish ─────────────────────────────────────────────────────────
    def test_keys_publish_no_token_configured(self):
        """Without RCLIPBOARD_ADMIN_TOKEN set server returns 503."""
        port = free_port()
        ctx = running_server(port=port)  # no admin token
        with ctx:
            status, body = post_json(
                f"http://127.0.0.1:{port}/v1/keys.publish",
                {"public_key": FAKE_KEY, "label": "test"},
                extra_headers={"Authorization": "Bearer anything"},
            )
            self.assertEqual(status, 503)
            self.assertEqual(body["code"], 5031)

    def test_keys_publish_wrong_token(self):
        status, body = post_json(
            f"{self._base()}/v1/keys.publish",
            {"public_key": FAKE_KEY, "label": "test"},
            extra_headers={"Authorization": "Bearer wrong-token"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], 4031)

    def test_keys_publish_no_auth_header(self):
        status, body = post_json(
            f"{self._base()}/v1/keys.publish",
            {"public_key": FAKE_KEY, "label": "test"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], 4031)

    def test_keys_publish_and_list(self):
        status, body = post_json(
            f"{self._base()}/v1/keys.publish",
            {"public_key": FAKE_KEY, "label": "laptop"},
            extra_headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn("key_id", body)

        status, body = self._list_keys()
        self.assertEqual(status, 200)
        self.assertEqual(len(body["keys"]), 1)
        self.assertEqual(body["keys"][0]["public_key"], FAKE_KEY)
        self.assertEqual(body["keys"][0]["label"], "laptop")

    # ── clip.get with encrypted content ──────────────────────────────────────
    def _put_encrypted(self, value: str = "ciphertext") -> None:
        post_json(
            f"{self._base()}/v1/clip.put",
            {
                "items": [
                    {
                        "topic": "c",
                        "mime": "application/octet-stream",
                        "encoding": "base64",
                        "value": value,
                        "encrypted": True,
                    }
                ],
                "meta": {},
            },
        )

    def test_clip_get_unencrypted_no_header(self):
        """Unencrypted content is freely accessible without any header."""
        post_json(
            f"{self._base()}/v1/clip.put",
            {
                "items": [{"topic": "c", "mime": "text/plain", "encoding": "utf-8", "value": "hello"}],
                "meta": {},
            },
        )
        status, body = post_json(f"{self._base()}/v1/clip.get", {"topic": "c"})
        self.assertEqual(status, 200)
        self.assertEqual(body["item"]["value"], "hello")

    def test_clip_get_encrypted_no_header(self):
        """Encrypted content without X-Age-Public-Key header → 403."""
        self._put_encrypted()
        status, body = post_json(f"{self._base()}/v1/clip.get", {"topic": "c"})
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], 4032)

    def test_clip_get_encrypted_unregistered_key(self):
        """Encrypted content with an unknown public key header → 403."""
        self._put_encrypted()
        status, body = post_json(
            f"{self._base()}/v1/clip.get",
            {"topic": "c"},
            extra_headers={"X-Age-Public-Key": OTHER_KEY},
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], 4032)

    def test_clip_get_encrypted_registered(self):
        """Registered key in header grants access to encrypted content."""
        # Register key
        post_json(
            f"{self._base()}/v1/keys.publish",
            {"public_key": FAKE_KEY, "label": "test"},
            extra_headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        self._put_encrypted("dGVzdA==")  # base64("test")
        status, body = post_json(
            f"{self._base()}/v1/clip.get",
            {"topic": "c"},
            extra_headers={"X-Age-Public-Key": FAKE_KEY},
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["item"]["encrypted"])

    # ── GET /v1/clip/{topic} with encrypted content ──────────────────────────
    def test_get_clip_topic_unencrypted_no_header(self):
        """Plain content stays freely accessible on the GET endpoint."""
        post_json(
            f"{self._base()}/v1/clip.put",
            {
                "items": [{"topic": "c", "mime": "text/plain", "encoding": "utf-8", "value": "hello"}],
                "meta": {},
            },
        )
        status, body = get_json(f"{self._base()}/v1/clip/c")
        self.assertEqual(status, 200)
        self.assertEqual(body["value"]["value"], "hello")

    def test_get_clip_topic_encrypted_no_header(self):
        """GET /v1/clip/{topic} enforces the same gate as POST /v1/clip.get."""
        self._put_encrypted()
        status, body = get_json(f"{self._base()}/v1/clip/c")
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], 4032)

    def test_get_clip_topic_encrypted_unregistered_key(self):
        self._put_encrypted()
        status, body = get_json(
            f"{self._base()}/v1/clip/c",
            extra_headers={"X-Age-Public-Key": OTHER_KEY},
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], 4032)

    def test_get_clip_topic_encrypted_registered(self):
        post_json(
            f"{self._base()}/v1/keys.publish",
            {"public_key": FAKE_KEY, "label": "test"},
            extra_headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        self._put_encrypted("dGVzdA==")
        status, body = get_json(
            f"{self._base()}/v1/clip/c",
            extra_headers={"X-Age-Public-Key": FAKE_KEY},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["value"]["value"], "dGVzdA==")

    # ── clip.watch initial sync with encrypted content ───────────────────────
    def _watch_contents(self, public_key: str | None) -> dict:
        """Open a WS, send clip.watch (optionally with a key), return contents."""
        import asyncio
        import json

        from websockets.asyncio.client import connect

        async def _run() -> dict:
            uri = f"ws://127.0.0.1:{self.port}/ws"
            async with connect(uri) as ws:
                params: dict = {"topics": ["c"]}
                if public_key is not None:
                    params["public_key"] = public_key
                await ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": 1,
                    "method": "clip.watch", "params": params,
                }))
                reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                return reply["result"].get("contents", {})

        return asyncio.run(_run())

    def test_watch_encrypted_not_replicated_without_key(self):
        """Policy: encrypted values are never pushed to peers without a
        registered key — the clip.watch initial sync included (previously it
        leaked them while clip.changed correctly filtered)."""
        self._put_encrypted()
        contents = self._watch_contents(public_key=None)
        self.assertNotIn("c", contents,
                         "encrypted topic must not appear in the watch reply "
                         "for a peer without a registered key")

    def test_watch_encrypted_not_replicated_with_unregistered_key(self):
        self._put_encrypted()
        contents = self._watch_contents(public_key=OTHER_KEY)
        self.assertNotIn("c", contents)

    def test_watch_encrypted_replicated_with_registered_key(self):
        post_json(
            f"{self._base()}/v1/keys.publish",
            {"public_key": FAKE_KEY, "label": "test"},
            extra_headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        self._put_encrypted("dGVzdA==")
        contents = self._watch_contents(public_key=FAKE_KEY)
        self.assertIn("c", contents)
        self.assertEqual(contents["c"]["value"]["value"], "dGVzdA==")

    def test_rewatch_encrypted_still_filtered_without_key(self):
        """The encrypted filter also applies to the re-watch reply (which
        returns contents for already-watched topics)."""
        import asyncio
        import json

        from websockets.asyncio.client import connect

        self._put_encrypted()

        async def _run() -> None:
            uri = f"ws://127.0.0.1:{self.port}/ws"
            async with connect(uri) as ws:
                for mid in (1, 2):
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0", "id": mid,
                        "method": "clip.watch",
                        "params": {"topics": ["c"]},
                    }))
                    reply = json.loads(
                        await asyncio.wait_for(ws.recv(), timeout=5))
                    contents = reply["result"].get("contents", {})
                    self.assertNotIn("c", contents,
                                     f"watch #{mid} must not leak encrypted")

        asyncio.run(_run())

    def test_watch_unencrypted_replicated_without_key(self):
        """Plain values keep flowing in the watch reply regardless of keys."""
        post_json(
            f"{self._base()}/v1/clip.put",
            {
                "items": [{"topic": "c", "mime": "text/plain",
                           "encoding": "utf-8", "value": "plain-ok"}],
                "meta": {},
            },
        )
        contents = self._watch_contents(public_key=None)
        self.assertIn("c", contents)
        self.assertEqual(contents["c"]["value"]["value"], "plain-ok")


if __name__ == "__main__":
    unittest.main()
