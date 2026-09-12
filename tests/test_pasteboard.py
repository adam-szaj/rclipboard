from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from base64 import b64encode
from pathlib import Path
from unittest import mock

from fastapi import FastAPI

from rclipboard.config import load_config
from rclipboard.models.wire import TopicData, ValueData
from rclipboard.transports import pasteboard


class PasteboardConfigTests(unittest.TestCase):
    def test_config_maps_pasteboard_fields_to_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(
                "[pasteboard]\n"
                "enabled = true\n"
                'pbcopy_path = "/custom/pbcopy"\n'
                'pbpaste_path = "/custom/pbpaste"\n'
                "interval_ms = 321\n"
            )

            self.assertEqual(
                {
                    "RCLIPBOARD_PASTEBOARD": "1",
                    "RCLIPBOARD_PBCOPY_PATH": "/custom/pbcopy",
                    "RCLIPBOARD_PBPASTE_PATH": "/custom/pbpaste",
                    "RCLIPBOARD_PASTEBOARD_INTERVAL_MS": "321",
                },
                load_config(config),
            )


class PasteboardAvailabilityTests(unittest.TestCase):
    def test_requires_darwin_and_executable_tools(self) -> None:
        with (
            mock.patch.object(pasteboard.sys, "platform", "darwin"),
            mock.patch.object(pasteboard.os, "access", return_value=True),
        ):
            self.assertTrue(pasteboard.tools_available())

        with (
            mock.patch.object(pasteboard.sys, "platform", "linux"),
            mock.patch.object(pasteboard.os, "access", return_value=True),
        ):
            self.assertFalse(pasteboard.tools_available())

        with (
            mock.patch.object(pasteboard.sys, "platform", "darwin"),
            mock.patch.object(pasteboard.os, "access", return_value=False),
        ):
            self.assertFalse(pasteboard.tools_available())


class PasteboardInterfaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.app = FastAPI()
        enabled = mock.patch.dict(
            os.environ, {"RCLIPBOARD_PASTEBOARD": "0"}
        )
        enabled.start()
        self.addCleanup(enabled.stop)
        self.conn = pasteboard.PasteboardInterface(self.app)

    def test_paths_are_read_when_adapter_is_created(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "RCLIPBOARD_PBCOPY_PATH": "/late/pbcopy",
                "RCLIPBOARD_PBPASTE_PATH": "/late/pbpaste",
            },
        ):
            conn = pasteboard.PasteboardInterface(self.app)

        self.assertEqual(conn.pbcopy_path, Path("/late/pbcopy"))
        self.assertEqual(conn.pbpaste_path, Path("/late/pbpaste"))

    async def test_enablement_is_read_when_adapter_is_created(self) -> None:
        with (
            mock.patch.dict(os.environ, {"RCLIPBOARD_PASTEBOARD": "1"}),
            mock.patch.object(pasteboard.sys, "platform", "darwin"),
            mock.patch.object(pasteboard.os, "access", return_value=True),
        ):
            conn = pasteboard.PasteboardInterface(self.app)

        self.assertTrue(conn.enabled)
        self.assertTrue(conn.good)
        await conn.shutdown()

    async def test_local_change_is_enqueued_as_clipboard_topic(self) -> None:
        with (
            mock.patch.object(
                pasteboard,
                "_exec",
                new=mock.AsyncMock(return_value=(0, b"hello", b"")),
            ),
            mock.patch.object(
                pasteboard,
                "enqueue_topic_data",
                new=mock.AsyncMock(),
            ) as enqueue,
        ):
            await self.conn.read_item()

        enqueue.assert_awaited_once()
        app, item = enqueue.await_args.args
        self.assertIs(app, self.app)
        self.assertEqual(item.topic, "c")
        self.assertEqual(item.meta, {"app": "pasteboard"})
        self.assertEqual(item.value.value_type, "binary")
        self.assertEqual(item.value.value_encoding, "base64")
        self.assertEqual(item.value.value, b64encode(b"hello").decode())
        self.assertIs(enqueue.await_args.kwargs["source"], self.conn)

    async def test_remote_change_is_written_with_pbcopy(self) -> None:
        item = TopicData(
            topic="c",
            value=ValueData(
                value=b64encode(b"remote value").decode(),
                type="binary",
                encoding="base64",
            ),
            meta={},
        )
        with mock.patch.object(
            pasteboard,
            "_exec",
            new=mock.AsyncMock(return_value=(0, b"", b"")),
        ) as execute:
            await self.conn.send(item)

        execute.assert_awaited_once_with(
            self.conn.pbcopy_path,
            input_data=b"remote value",
            timeout=2.5,
        )
        self.assertEqual(self.conn.state.applied, b"remote value")
        self.assertEqual(self.conn.state.seen, b"remote value")
        self.assertIsNone(self.conn.last_error)

    async def test_remote_write_echo_is_not_enqueued(self) -> None:
        item = TopicData(
            topic="c",
            value=ValueData(value="remote value"),
            meta={},
        )
        with mock.patch.object(
            pasteboard,
            "_exec",
            new=mock.AsyncMock(return_value=(0, b"", b"")),
        ):
            await self.conn.send(item)

        with (
            mock.patch.object(
                pasteboard,
                "_exec",
                new=mock.AsyncMock(return_value=(0, b"remote value", b"")),
            ),
            mock.patch.object(
                pasteboard,
                "enqueue_topic_data",
                new=mock.AsyncMock(),
            ) as enqueue,
        ):
            await self.conn.read_item()

        enqueue.assert_not_awaited()

    async def test_non_clipboard_topic_is_ignored(self) -> None:
        item = TopicData(
            topic="p",
            value=ValueData(value="not supported"),
            meta={},
        )
        with mock.patch.object(
            pasteboard,
            "_exec",
            new=mock.AsyncMock(),
        ) as execute:
            await self.conn.send(item)

        execute.assert_not_awaited()

    async def test_command_failure_updates_status_without_enqueuing(
        self,
    ) -> None:
        with (
            mock.patch.object(
                pasteboard,
                "_exec",
                new=mock.AsyncMock(
                    return_value=(1, b"", b"pasteboard denied")
                ),
            ),
            mock.patch.object(
                pasteboard,
                "enqueue_topic_data",
                new=mock.AsyncMock(),
            ) as enqueue,
        ):
            await self.conn.read_item()

        enqueue.assert_not_awaited()
        self.assertEqual(self.conn.last_error, "pasteboard denied")

    async def test_exec_terminates_process_on_timeout(self) -> None:
        blocker = asyncio.Event()
        process = mock.Mock(returncode=None)

        async def communicate(*, input=None):
            del input
            await blocker.wait()
            return b"", b""

        process.communicate = communicate
        process.wait = mock.AsyncMock(return_value=0)

        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(
                pasteboard.a,
                "create_subprocess_exec",
                new=mock.AsyncMock(return_value=process),
            ) as create_process,
        ):
            result = await pasteboard._exec(
                Path("/usr/bin/pbpaste"), timeout=0.001
            )

        self.assertEqual(result, (124, b"", b"timeout"))
        process.terminate.assert_called_once_with()
        command_env = create_process.await_args.kwargs["env"]
        self.assertEqual(command_env["LC_CTYPE"], "UTF-8")


class PasteboardLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.app = FastAPI()

    def test_install_registers_and_subscribes_enabled_adapter(self) -> None:
        conn = mock.Mock(enabled=True)
        with (
            mock.patch.dict(os.environ, {"RCLIPBOARD_PASTEBOARD": "1"}),
            mock.patch.object(
                pasteboard, "PasteboardInterface", return_value=conn
            ),
            mock.patch.object(pasteboard, "register_client") as register,
            mock.patch.object(pasteboard, "subscribe_client") as subscribe,
        ):
            pasteboard.install_pasteboard(self.app)

        self.assertIs(self.app.state.pasteboard, conn)
        register.assert_called_once_with(self.app, conn)
        subscribe.assert_called_once_with(self.app, conn, ["c"])

    def test_install_skips_adapter_when_not_configured(self) -> None:
        with (
            mock.patch.dict(os.environ, {"RCLIPBOARD_PASTEBOARD": "0"}),
            mock.patch.object(pasteboard, "PasteboardInterface") as adapter,
        ):
            pasteboard.install_pasteboard(self.app)

        self.assertIsNone(self.app.state.pasteboard)
        adapter.assert_not_called()

    async def test_shutdown_stops_installed_adapter(self) -> None:
        conn = mock.Mock()
        conn.shutdown = mock.AsyncMock()
        self.app.state.pasteboard = conn

        await pasteboard.shutdown_pasteboard(self.app)

        conn.shutdown.assert_awaited_once_with()

    def test_status_has_stable_shape_when_adapter_is_disabled(self) -> None:
        self.app.state.pasteboard = None

        status = pasteboard.get_pasteboard_status(self.app)

        self.assertFalse(status["enabled"])
        self.assertFalse(status["good"])
        self.assertEqual(status["topics"], ["c"])
        self.assertEqual(status["pbcopy_path"], "/usr/bin/pbcopy")
        self.assertEqual(status["pbpaste_path"], "/usr/bin/pbpaste")


if __name__ == "__main__":
    unittest.main()
