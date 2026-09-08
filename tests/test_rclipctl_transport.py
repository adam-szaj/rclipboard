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

    def test_rclipctl_loads_config_from_xdg_path_with_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            config_home = home / "custom config"
            app_dir = config_home / "rclipboard"
            fake_rclipboard = app_dir / "venv/bin/rclipboard"
            fake_rclipboard.parent.mkdir(parents=True)
            (app_dir / "config.toml").write_text("[server]\n")
            config_log = home / "config.log"
            fake_rclipboard.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$4\" > \"$CONFIG_LOG\"\n"
                "printf 'RCLIPCTL_TRANSPORT=\"tcp\"\\n'\n"
            )
            fake_rclipboard.chmod(0o755)

            fake_bin = home / "fake-bin"
            fake_bin.mkdir()
            pidof = fake_bin / "pidof"
            pidof.write_text("#!/bin/sh\nexit 0\n")
            pidof.chmod(0o755)

            result = subprocess.run(
                [str(ROOT_DIR / "scripts/bin/rclipctl"), "unregister"],
                cwd=ROOT_DIR,
                env={
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                    "HOME": str(home),
                    "XDG_CONFIG_HOME": str(config_home),
                    "CONFIG_LOG": str(config_log),
                },
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                config_log.read_text().strip(),
                str(app_dir / "config.toml"),
            )

    def test_rcliptunel_loads_config_from_xdg_path_with_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            config_home = home / "custom config"
            app_dir = config_home / "rclipboard"
            fake_rclipboard = app_dir / "venv/bin/rclipboard"
            fake_rclipboard.parent.mkdir(parents=True)
            (app_dir / "config.toml").write_text("[server]\n")
            config_log = home / "config.log"
            fake_rclipboard.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$4\" > \"$CONFIG_LOG\"\n"
                "printf 'RCLIPBOARD_ENDPOINT=\"uds:///tmp/custom.sock\"\\n'\n"
            )
            fake_rclipboard.chmod(0o755)

            fake_bin = home / "fake-bin"
            fake_bin.mkdir()
            ssh_log = home / "ssh.log"
            ssh = fake_bin / "ssh"
            ssh.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" > \"$SSH_LOG\"\n"
            )
            ssh.chmod(0o755)

            result = subprocess.run(
                [
                    str(ROOT_DIR / "scripts/bin/rcliptunel"),
                    "--ssh",
                    "example.test",
                ],
                cwd=ROOT_DIR,
                env={
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                    "HOME": str(home),
                    "XDG_CONFIG_HOME": str(config_home),
                    "CONFIG_LOG": str(config_log),
                    "SSH_LOG": str(ssh_log),
                },
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                config_log.read_text().strip(),
                str(app_dir / "config.toml"),
            )
            self.assertIn("uds:///tmp/custom.sock", result.stdout)


if __name__ == "__main__":
    unittest.main()
