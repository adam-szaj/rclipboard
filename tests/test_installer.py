from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent


class InstallerTests(unittest.TestCase):
    def test_user_systemd_installer_creates_bin_venv_and_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            fake_bin = tmp_path / "fake-bin"
            home.mkdir()
            fake_bin.mkdir()

            systemctl_log = tmp_path / "systemctl.log"
            fake_systemctl = fake_bin / "systemctl"
            fake_systemctl.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                f"printf '%s\\n' \"$*\" >> {systemctl_log!s}\n"
            )
            fake_systemctl.chmod(0o755)

            env = os.environ.copy()
            env["HOME"] = str(home)
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["RCLIPBOARD_INSTALL_SKIP_PIP"] = "1"

            subprocess.run(
                ["bash", str(ROOT_DIR / "scripts/install-systemd-user.sh"), str(ROOT_DIR)],
                check=True,
                cwd=ROOT_DIR,
                env=env,
            )

            app_dir = home / ".config" / "rclipboard"
            bin_dir = app_dir / "bin"
            venv_dir = app_dir / "venv"
            unit_dir = home / ".config" / "systemd" / "user"

            self.assertTrue(bin_dir.is_dir())
            self.assertTrue(venv_dir.is_dir())
            self.assertTrue((venv_dir / "bin" / "python").exists())
            self.assertTrue((app_dir / "env").exists())

            for name in ["rclipctl", "rctrl-c", "rctrl-v", "rclip-smoke.sh"]:
                path = bin_dir / name
                self.assertTrue(path.exists(), name)
                self.assertTrue(os.access(path, os.X_OK), name)

            service = (unit_dir / "rclipboard.service").read_text()
            proxy_service = (unit_dir / "rclipboard-proxy.service").read_text()
            fd_service = (unit_dir / "rclipboard@.service").read_text()

            self.assertIn("WorkingDirectory=%h/.config/rclipboard", service)
            self.assertIn(
                "ExecStart=%h/.config/rclipboard/venv/bin/rclipboard",
                service,
            )
            self.assertIn(
                "ExecStart=%h/.config/rclipboard/venv/bin/rclipboard",
                proxy_service,
            )
            self.assertIn(
                "ExecStart=%h/.config/rclipboard/venv/bin/rclipboard --fd 3",
                fd_service,
            )

            override = (unit_dir / "rclipboard.service.d" / "override.conf").read_text()
            self.assertIn(f"WorkingDirectory={app_dir}", override)

            self.assertTrue(systemctl_log.exists())
            self.assertIn("--user daemon-reload", systemctl_log.read_text())


if __name__ == "__main__":
    unittest.main()
