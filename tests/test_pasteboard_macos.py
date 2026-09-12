"""Native macOS integration tests for pbcopy/pbpaste.

These tests intentionally use the real user pasteboard. They are selected by
``make test-platform-clipboard`` only on Darwin and restore the original text
content after each test.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

from fastapi import FastAPI

from rclipboard.models.wire import TopicData, ValueData
from rclipboard.transports import pasteboard


@unittest.skipUnless(sys.platform == "darwin", "requires native macOS")
class NativePasteboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.assertTrue(os.access(pasteboard.DEFAULT_PBCOPY_PATH, os.X_OK))
        self.assertTrue(os.access(pasteboard.DEFAULT_PBPASTE_PATH, os.X_OK))

        code, original, stderr = await pasteboard._exec(
            pasteboard.DEFAULT_PBPASTE_PATH, timeout=2.5
        )
        self.assertEqual(code, 0, stderr.decode(errors="replace"))
        self.original = original
        self.addAsyncCleanup(self._restore_pasteboard)

        self.env = mock.patch.dict(
            os.environ,
            {
                "RCLIPBOARD_PASTEBOARD": "0",
                "RCLIPBOARD_PBCOPY_PATH": str(
                    pasteboard.DEFAULT_PBCOPY_PATH
                ),
                "RCLIPBOARD_PBPASTE_PATH": str(
                    pasteboard.DEFAULT_PBPASTE_PATH
                ),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.conn = pasteboard.PasteboardInterface(FastAPI())

    async def _restore_pasteboard(self) -> None:
        code, _, stderr = await pasteboard._exec(
            pasteboard.DEFAULT_PBCOPY_PATH,
            input_data=self.original,
            timeout=2.5,
        )
        self.assertEqual(code, 0, stderr.decode(errors="replace"))

    async def test_local_pbcopy_is_read_and_enqueued(self) -> None:
        expected = "rclipboard lokalny: Zażółć gęślą jaźń 🦊".encode()
        code, _, stderr = await pasteboard._exec(
            pasteboard.DEFAULT_PBCOPY_PATH,
            input_data=expected,
            timeout=2.5,
        )
        self.assertEqual(code, 0, stderr.decode(errors="replace"))

        with mock.patch.object(
            pasteboard,
            "enqueue_topic_data",
            new=mock.AsyncMock(),
        ) as enqueue:
            await self.conn.read_item()

        enqueue.assert_awaited_once()
        _, item = enqueue.await_args.args
        self.assertEqual(item.topic, "c")
        self.assertEqual(item.value.value_type, "binary")
        self.assertEqual(item.value.value_encoding, "base64")
        self.assertEqual(pasteboard._item_bytes(item), expected)
        self.assertIs(enqueue.await_args.kwargs["source"], self.conn)

    async def test_remote_item_is_written_and_echo_is_suppressed(self) -> None:
        expected = "rclipboard zdalny: Pchnąć w tę łódź jeża 🐟"
        await self.conn.send(
            TopicData(
                topic="c",
                value=ValueData(value=expected),
                meta={},
            )
        )

        code, actual, stderr = await pasteboard._exec(
            pasteboard.DEFAULT_PBPASTE_PATH, timeout=2.5
        )
        self.assertEqual(code, 0, stderr.decode(errors="replace"))
        self.assertEqual(actual, expected.encode())

        with mock.patch.object(
            pasteboard,
            "enqueue_topic_data",
            new=mock.AsyncMock(),
        ) as enqueue:
            await self.conn.read_item()

        enqueue.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
