from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT_DIR, free_port, running_server


class RclipctlTransportTests(unittest.TestCase):
    def _run_rclipctl(self, home: Path, endpoint: str,
                      *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(ROOT_DIR / "scripts" / "bin" / "rclipctl"), *args],
            cwd=ROOT_DIR,
            env={"PATH": "/usr/bin:/bin", "HOME": str(home),
                 "RCLIPBOARD_ENDPOINT": endpoint},
            text=True,
            capture_output=True,
            check=True,
        )

    def test_transport_flag_can_force_tcp(self):
        port = free_port()
        with tempfile.TemporaryDirectory() as tmp:
            with running_server(port=port):
                proc = self._run_rclipctl(Path(tmp), f"127.0.0.1:{port}",
                                          "health", "--transport", "tcp")
                self.assertIn('"ok":true', proc.stdout.replace(" ", ""))


if __name__ == "__main__":
    unittest.main()
