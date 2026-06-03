"""Unit tests for the xsel display env-file mechanism.

The display env-file is written by the session-bound rclipboard-display.service
and re-read by xsel on every invocation, so the server picks up DISPLAY without
a restart after the graphical session comes up.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rclipboard import xsel


class LoadDisplayEnvTests(unittest.TestCase):
    def _write(self, text: str) -> Path:
        fd, name = tempfile.mkstemp(prefix="display_env_")
        os.close(fd)
        path = Path(name)
        path.write_text(text)
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def test_missing_file_returns_empty(self):
        path = Path(tempfile.gettempdir()) / "definitely-not-here-12345.env"
        self.assertEqual(xsel.load_display_env(path), {})

    def test_parses_plain_keyvalue(self):
        path = self._write("DISPLAY=:0\nWAYLAND_DISPLAY=wayland-0\n")
        env = xsel.load_display_env(path)
        self.assertEqual(env["DISPLAY"], ":0")
        self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-0")

    def test_strips_quotes_and_export_prefix(self):
        path = self._write(
            'export DISPLAY=":0"\n'
            "XAUTHORITY='/run/user/1001/.mutter-Xwaylandauth.ABC'\n"
        )
        env = xsel.load_display_env(path)
        self.assertEqual(env["DISPLAY"], ":0")
        self.assertEqual(
            env["XAUTHORITY"], "/run/user/1001/.mutter-Xwaylandauth.ABC"
        )

    def test_ignores_comments_blank_and_unknown_keys(self):
        path = self._write(
            "# a comment\n"
            "\n"
            "DISPLAY=:1\n"
            "SOMETHING_ELSE=should-be-ignored\n"
            "MALFORMED_LINE_NO_EQUALS\n"
        )
        env = xsel.load_display_env(path)
        self.assertEqual(env, {"DISPLAY": ":1"})

    def test_empty_value_skipped(self):
        path = self._write("DISPLAY=\nWAYLAND_DISPLAY=wayland-0\n")
        env = xsel.load_display_env(path)
        self.assertNotIn("DISPLAY", env)
        self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-0")


class EffectiveDisplayTests(unittest.TestCase):
    def test_display_from_file_overrides_absent_process_env(self):
        path = Path(tempfile.gettempdir()) / "rclip-test-eff.env"
        path.write_text("DISPLAY=:7\n")
        self.addCleanup(path.unlink, missing_ok=True)
        with mock.patch.object(xsel, "DISPLAY_ENV_FILE", path), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(xsel._display_available())
            self.assertEqual(xsel._xsel_env().get("DISPLAY"), ":7")

    def test_file_display_overrides_process_env(self):
        path = Path(tempfile.gettempdir()) / "rclip-test-eff2.env"
        path.write_text("DISPLAY=:9\n")
        self.addCleanup(path.unlink, missing_ok=True)
        with mock.patch.object(xsel, "DISPLAY_ENV_FILE", path), \
                mock.patch.dict(os.environ, {"DISPLAY": ":0"}, clear=True):
            self.assertEqual(xsel._xsel_env().get("DISPLAY"), ":9")

    def test_no_file_falls_back_to_process_env(self):
        path = Path(tempfile.gettempdir()) / "rclip-test-absent.env"
        path.unlink(missing_ok=True)
        with mock.patch.object(xsel, "DISPLAY_ENV_FILE", path), \
                mock.patch.dict(os.environ, {"DISPLAY": ":0"}, clear=True):
            self.assertTrue(xsel._display_available())
            self.assertEqual(xsel._xsel_env().get("DISPLAY"), ":0")

    def test_no_display_anywhere(self):
        path = Path(tempfile.gettempdir()) / "rclip-test-absent2.env"
        path.unlink(missing_ok=True)
        with mock.patch.object(xsel, "DISPLAY_ENV_FILE", path), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(xsel._display_available())


class ReadSelectionGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_selection_skips_without_display(self):
        path = Path(tempfile.gettempdir()) / "rclip-test-gate.env"
        path.unlink(missing_ok=True)
        with mock.patch.object(xsel, "DISPLAY_ENV_FILE", path), \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(xsel, "_exec") as exec_mock:
            out = await xsel.read_selection("-b", timeout=1)
            self.assertEqual(out, b"")
            exec_mock.assert_not_called()

    async def test_read_selection_runs_with_file_display(self):
        path = Path(tempfile.gettempdir()) / "rclip-test-gate2.env"
        path.write_text("DISPLAY=:3\n")
        self.addCleanup(path.unlink, missing_ok=True)

        async def fake_exec(*_args, **kwargs):
            # DISPLAY from the file must reach the subprocess env.
            self.assertEqual(kwargs["env"].get("DISPLAY"), ":3")
            return (0, b"clip-data", b"")

        with mock.patch.object(xsel, "DISPLAY_ENV_FILE", path), \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(xsel, "XSEL_ENCRYPT", False), \
                mock.patch.object(xsel, "_exec", side_effect=fake_exec):
            out = await xsel.read_selection("-b", timeout=1)
            self.assertEqual(out, b"clip-data")


if __name__ == "__main__":
    unittest.main()
