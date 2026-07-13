"""Key-registration propagation proxy -> upstream.

Policy: encrypted values are only pushed to peers with a registered key.
A proxy therefore has to make its clients' keys known upstream:
- keys registered at the proxy BEFORE it connects are replayed on connect
  (before clip.watch, so the initial sync already includes encrypted topics),
- keys registered WHILE connected are forwarded live.
"""
from __future__ import annotations

import time
import unittest

from tests.helpers import (
    free_port,
    get_json,
    post_json,
    running_server,
)

FAKE_KEY = "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p"
OTHER_KEY = "age1qj2c3y4p5r6s7t8u9v0w1x2y3z4a5b6c7d8e9f0g1h2i3j4k5l6m7n8o9p"
ADMIN_TOKEN = "test-admin-token"
CIPHER_B64 = "Q0lQSEVSVEVYVA=="  # base64("CIPHERTEXT")


def _register_key(port: int, key: str, label: str) -> tuple[int, object]:
    return post_json(
        f"http://127.0.0.1:{port}/v1/keys.publish",
        {"public_key": key, "label": label},
        extra_headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )


def _put_encrypted(port: int, value_b64: str) -> tuple[int, object]:
    return post_json(
        f"http://127.0.0.1:{port}/v1/clip.put",
        {
            "items": [
                {"topic": "c", "mime": "application/octet-stream",
                 "encoding": "base64", "value": value_b64, "encrypted": True}
            ],
            "meta": {},
        },
    )


def _keys_on(port: int) -> list[str]:
    status, body = get_json(f"http://127.0.0.1:{port}/v1/keys.list")
    assert status == 200, body
    assert isinstance(body, dict)
    return [k["public_key"] for k in body["keys"]]


def _wait_for(predicate, timeout: float = 10.0, what: str = "condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}, last={last!r}")


class KeyPropagationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream_port = free_port()
        self.proxy_port = free_port()
        self.upstream_ctx = running_server(
            port=self.upstream_port,
            extra_env={"RCLIPBOARD_ADMIN_TOKEN": ADMIN_TOKEN},
        )
        self.upstream_ctx.__enter__()
        # Proxy starts WITHOUT an upstream — connected later via proxy.connect,
        # reproducing the race: clients register before the upstream link exists.
        self.proxy_ctx = running_server(
            port=self.proxy_port,
            extra_env={
                "RCLIPBOARD_ADMIN_TOKEN": ADMIN_TOKEN,
                "RCLIPBOARD_UPSTREAM_ADMIN_TOKEN": ADMIN_TOKEN,
            },
        )
        self.proxy_ctx.__enter__()

    def tearDown(self) -> None:
        self.proxy_ctx.__exit__(None, None, None)
        self.upstream_ctx.__exit__(None, None, None)

    def _connect_proxy(self) -> None:
        status, body = post_json(
            f"http://127.0.0.1:{self.proxy_port}/v1/proxy.connect",
            {"endpoint": f"127.0.0.1:{self.upstream_port}"},
            extra_headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        self.assertEqual(status, 200, body)

    def test_keys_registered_before_connect_are_replayed(self):
        # encrypted value already sits on the upstream
        status, _ = _put_encrypted(self.upstream_port, CIPHER_B64)
        self.assertEqual(status, 200)
        # client registers at the proxy BEFORE the upstream link exists
        status, _ = _register_key(self.proxy_port, FAKE_KEY, "laptop")
        self.assertEqual(status, 200)

        self._connect_proxy()

        # replay: the key must appear in the upstream registry
        _wait_for(lambda: FAKE_KEY in _keys_on(self.upstream_port),
                  what="key replayed to upstream")

        # and the encrypted topic must reach the proxy via the initial sync
        def _proxy_has_cipher():
            status, body = post_json(
                f"http://127.0.0.1:{self.proxy_port}/v1/clip.get",
                {"topic": "c"},
                extra_headers={"X-Age-Public-Key": FAKE_KEY},
            )
            return (status == 200 and isinstance(body, dict)
                    and body["item"]["value"] == CIPHER_B64)

        _wait_for(_proxy_has_cipher, what="encrypted topic replicated to proxy")

    def test_key_registered_while_connected_is_forwarded(self):
        self._connect_proxy()
        _wait_for(
            lambda: get_json(
                f"http://127.0.0.1:{self.proxy_port}/v1/health.get"
            )[1].get("proxy_good") is True,  # type: ignore[union-attr]
            what="proxy connected",
        )

        status, _ = _register_key(self.proxy_port, OTHER_KEY, "desktop")
        self.assertEqual(status, 200)

        _wait_for(lambda: OTHER_KEY in _keys_on(self.upstream_port),
                  what="live key forwarded to upstream")

    def test_encrypted_update_flows_after_replay(self):
        # key known at the proxy, then connect, then a NEW encrypted value
        # appears upstream — the clip.changed path must now pass the filter.
        status, _ = _register_key(self.proxy_port, FAKE_KEY, "laptop")
        self.assertEqual(status, 200)
        self._connect_proxy()
        _wait_for(lambda: FAKE_KEY in _keys_on(self.upstream_port),
                  what="key replayed to upstream")

        new_cipher = "TkVXLUNJUEhFUg=="  # base64("NEW-CIPHER")
        status, _ = _put_encrypted(self.upstream_port, new_cipher)
        self.assertEqual(status, 200)

        def _proxy_has_new():
            status, body = post_json(
                f"http://127.0.0.1:{self.proxy_port}/v1/clip.get",
                {"topic": "c"},
                extra_headers={"X-Age-Public-Key": FAKE_KEY},
            )
            return (status == 200 and isinstance(body, dict)
                    and body["item"]["value"] == new_cipher)

        _wait_for(_proxy_has_new, what="encrypted update replicated to proxy")


if __name__ == "__main__":
    unittest.main()
