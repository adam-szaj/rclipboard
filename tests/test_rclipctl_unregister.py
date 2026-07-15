"""rclipctl unregister: local keypair removal stub (server-side is a TODO)."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT_DIR


class RclipctlUnregisterTests(unittest.TestCase):
    def _run(self, home: Path) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["XDG_CONFIG_HOME"] = str(home / ".config")
        return subprocess.run(
            [str(ROOT_DIR / "scripts" / "bin" / "rclipctl"), "unregister"],
            env=env, capture_output=True, text=True,
        )

    def test_unregister_removes_local_keypair_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cfg = home / ".config" / "rclipboard"
            cfg.mkdir(parents=True)
            (cfg / "age_key.txt").write_text("AGE-SECRET-KEY-1TEST\n")
            (cfg / "age_key.pub").write_text("age1testpub\n")

            proc = self._run(home)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse((cfg / "age_key.txt").exists())
            self.assertFalse((cfg / "age_key.pub").exists())

    def test_unregister_without_keypair_still_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".config" / "rclipboard").mkdir(parents=True)
            proc = self._run(home)
            self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
