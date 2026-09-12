from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rclipboard.config import load_config


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


if __name__ == "__main__":
    unittest.main()
