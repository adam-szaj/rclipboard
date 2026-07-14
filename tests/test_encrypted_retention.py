"""Time-based retention for encrypted messages.

Policy (user decision): an encrypted item (meta["encrypted"]=true) expires
RCLIPBOARD_ENCRYPTED_TTL_S seconds after its meta["ts"] (write time), default
30 s. On expiry the stored VALUE is replaced by the empty string — the topic
itself keeps existing (encrypted flag preserved), it is NOT a 404. Expiry is
lazy: it happens when a read path touches the item, and it mutates the store
so the ciphertext is dropped from RAM on first touch. Plain (unencrypted)
items are never affected.
"""
from __future__ import annotations

import datetime
import unittest
from unittest import mock

from rclipboard.core.topics import InternalTopicData
from rclipboard.models.wire import TopicData, ValueData
from rclipboard.timeutil import utc_timestamp

UTC = datetime.timezone.utc


def _itd(value: str, *, ts: str | None, encrypted: bool) -> InternalTopicData:
    meta: dict = {}
    if ts is not None:
        meta["ts"] = ts
    if encrypted:
        meta["encrypted"] = True
    td = TopicData(
        topic="c",
        value=ValueData(value=value, type="binary", encoding="base64"),
        meta=meta,
    )
    return InternalTopicData(data=td, source=None)


def _ts_ago(seconds: float) -> str:
    dt = datetime.datetime.now(UTC) - datetime.timedelta(seconds=seconds)
    return dt.isoformat()


class ExpireItemTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from rclipboard.core.state import AppState
        app = mock.MagicMock()
        with mock.patch.dict("os.environ", {"RCLIPBOARD_ENCRYPTED_TTL_S": "30"}):
            self.state = AppState(app)

    async def asyncTearDown(self):
        import asyncio
        import contextlib
        self.state.dispatcher_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.state.dispatcher_task

    def _expire(self, itd):
        return self.state._expire_if_needed(itd)

    def test_fresh_encrypted_not_expired(self):
        itd = _itd("Q0lQSA==", ts=_ts_ago(1), encrypted=True)
        self.assertFalse(self._expire(itd))
        self.assertEqual(itd.data.value.value, "Q0lQSA==")

    def test_old_encrypted_expired_to_empty_string(self):
        itd = _itd("Q0lQSA==", ts=_ts_ago(60), encrypted=True)
        self.assertTrue(self._expire(itd))
        self.assertEqual(itd.data.value.value, "")
        # topic still marked encrypted; not deleted / not a 404
        self.assertTrue(itd.data.meta.get("encrypted"))
        self.assertTrue(itd.data.meta.get("expired"))

    def test_plain_item_never_expires(self):
        itd = _itd("hello", ts=_ts_ago(9999), encrypted=False)
        self.assertFalse(self._expire(itd))
        self.assertEqual(itd.data.value.value, "hello")

    def test_expiry_is_idempotent(self):
        itd = _itd("Q0lQSA==", ts=_ts_ago(60), encrypted=True)
        self.assertTrue(self._expire(itd))
        # second touch: already empty, reports not-newly-expired, stays empty
        self.assertFalse(self._expire(itd))
        self.assertEqual(itd.data.value.value, "")

    def test_encrypted_without_ts_never_expires(self):
        # legacy item with no comparable timestamp: cannot expire it
        itd = _itd("Q0lQSA==", ts=None, encrypted=True)
        self.assertFalse(self._expire(itd))
        self.assertEqual(itd.data.value.value, "Q0lQSA==")

    def test_ttl_boundary(self):
        # just inside TTL survives; just outside expires
        self.assertFalse(self._expire(_itd("x", ts=_ts_ago(29), encrypted=True)))
        self.assertTrue(self._expire(_itd("x", ts=_ts_ago(31), encrypted=True)))


class TtlDisabledTests(unittest.IsolatedAsyncioTestCase):
    """ttl=0 disables the feature entirely — nothing ever expires."""

    async def asyncSetUp(self):
        from rclipboard.core.state import AppState
        app = mock.MagicMock()
        with mock.patch.dict("os.environ", {"RCLIPBOARD_ENCRYPTED_TTL_S": "0"}):
            self.state = AppState(app)

    async def asyncTearDown(self):
        import asyncio
        import contextlib
        self.state.dispatcher_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.state.dispatcher_task

    def test_disabled_never_expires_however_old(self):
        itd = _itd("Q0lQSA==", ts=_ts_ago(99999), encrypted=True)
        self.assertFalse(self.state._expire_if_needed(itd))
        self.assertEqual(itd.data.value.value, "Q0lQSA==")


class TtlConfigTests(unittest.IsolatedAsyncioTestCase):
    async def _state_with(self, env: dict):
        from rclipboard.core.state import AppState
        app = mock.MagicMock()
        with mock.patch.dict("os.environ", env, clear=False):
            return AppState(app)

    async def _teardown(self, state):
        import asyncio
        import contextlib
        state.dispatcher_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await state.dispatcher_task

    async def test_default_ttl_is_30s(self):
        env = {k: v for k, v in __import__("os").environ.items()
               if k != "RCLIPBOARD_ENCRYPTED_TTL_S"}
        with mock.patch.dict("os.environ", env, clear=True):
            state = await self._state_with({})
        try:
            self.assertEqual(state.encrypted_ttl.total_seconds(), 30)
        finally:
            await self._teardown(state)

    async def test_custom_ttl(self):
        state = await self._state_with({"RCLIPBOARD_ENCRYPTED_TTL_S": "5"})
        try:
            self.assertEqual(state.encrypted_ttl.total_seconds(), 5)
        finally:
            await self._teardown(state)


FAKE_KEY = "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p"
ADMIN_TOKEN = "test-admin-token"
CIPHER_B64 = "Q0lQSEVSVEVYVA=="  # base64("CIPHERTEXT")


class EncryptedRetentionHttpTests(unittest.TestCase):
    """End-to-end: encrypted value read back before/after a short TTL."""

    def setUp(self) -> None:
        from tests.helpers import free_port, running_server
        self.port = free_port()
        self.ctx = running_server(
            port=self.port,
            extra_env={
                "RCLIPBOARD_ADMIN_TOKEN": ADMIN_TOKEN,
                "RCLIPBOARD_ENCRYPTED_TTL_S": "1",
            },
        )
        self.ctx.__enter__()

    def tearDown(self) -> None:
        self.ctx.__exit__(None, None, None)

    def _base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _register(self) -> None:
        from tests.helpers import post_json
        post_json(
            f"{self._base()}/v1/keys.publish",
            {"public_key": FAKE_KEY, "label": "t"},
            extra_headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )

    def _put_encrypted(self, value: str) -> None:
        from tests.helpers import post_json
        post_json(
            f"{self._base()}/v1/clip.put",
            {"items": [{"topic": "c", "mime": "application/octet-stream",
                        "encoding": "base64", "value": value,
                        "encrypted": True}], "meta": {}},
        )

    def _get(self):
        from tests.helpers import post_json
        return post_json(
            f"{self._base()}/v1/clip.get", {"topic": "c"},
            extra_headers={"X-Age-Public-Key": FAKE_KEY},
        )

    def test_encrypted_value_expires_to_empty_string(self):
        import time
        self._register()
        self._put_encrypted(CIPHER_B64)

        status, body = self._get()
        self.assertEqual(status, 200)
        self.assertEqual(body["item"]["value"], CIPHER_B64)
        self.assertTrue(body["item"]["encrypted"])

        time.sleep(1.5)  # exceed the 1 s TTL

        status, body = self._get()
        # NOT a 404 — the topic survives, value blanked, flag preserved
        self.assertEqual(status, 200)
        self.assertEqual(body["item"]["value"], "")
        self.assertTrue(body["item"]["encrypted"])

    def test_plain_value_does_not_expire(self):
        import time
        from tests.helpers import post_json
        post_json(
            f"{self._base()}/v1/clip.put",
            {"items": [{"topic": "p", "mime": "text/plain",
                        "encoding": "utf-8", "value": "keepme"}], "meta": {}},
        )
        time.sleep(1.5)
        status, body = post_json(f"{self._base()}/v1/clip.get", {"topic": "p"})
        self.assertEqual(status, 200)
        self.assertEqual(body["item"]["value"], "keepme")


if __name__ == "__main__":
    _ = utc_timestamp
    unittest.main()
