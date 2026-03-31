from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.helpers import (
    free_port,
    post_json,
    running_fifo_server,
    running_server,
    wait_for_file_bytes,
    wait_for_path,
)


class FunctionalFIFOTests(unittest.TestCase):
    def test_raw_and_json_fifo_put_update_state_snapshots(self):
        with running_fifo_server() as (root, _proc):
            put_c = root / "put.c.fifo"
            state_c = root / "state.c"
            wait_for_path(put_c)

            with put_c.open("wb") as f:
                f.write(b"hello-fifo")
            wait_for_file_bytes(state_c, b"hello-fifo")

            state_c_json = json.loads((root / "state.c.json").read_text())
            self.assertEqual(state_c_json["item"]["topic"], "c")
            self.assertEqual(state_c_json["item"]["encoding"], "base64")

            put_p_json = root / "put.p.fifo.json"
            wait_for_path(put_p_json)
            put_p_json.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "topic": "p",
                                "mime": "text/plain",
                                "encoding": "utf-8",
                                "value": "hello-json",
                            }
                        ],
                        "meta": {"app": "fifo-test"},
                    }
                )
            )
            wait_for_file_bytes(root / "state.p", b"hello-json")

            topics = json.loads((root / "topics.json").read_text())
            self.assertIn("c", topics["topics"])
            self.assertIn("p", topics["topics"])

    def test_fifo_runs_in_parallel_with_http(self):
        port = free_port()
        with tempfile.TemporaryDirectory() as tmp:
            root = tmp
            with running_server(
                port=port,
                extra_env={"RCLIPBOARD_FIFO_DIR": root},
            ):
                wait_for_path(Path(root) / "health.json")

                status, body = post_json(
                    f"http://127.0.0.1:{port}/v1/clip.put",
                    {
                        "items": [
                            {
                                "topic": "c",
                                "mime": "text/plain",
                                "encoding": "utf-8",
                                "value": "from-http",
                            }
                        ],
                        "meta": {"app": "http-test"},
                    },
                )
                self.assertEqual(status, 200)
                wait_for_file_bytes(Path(root) / "state.c", b"from-http")

                put_fifo = Path(root) / "put.p.fifo"
                wait_for_path(put_fifo)
                with put_fifo.open("wb") as f:
                    f.write(b"from-fifo")
                wait_for_file_bytes(Path(root) / "state.p", b"from-fifo")

                status, body = post_json(
                    f"http://127.0.0.1:{port}/v1/clip.get",
                    {"topic": "p"},
                )
                self.assertEqual(status, 200)
                self.assertEqual(body["item"]["value"], "ZnJvbS1maWZv")
                self.assertEqual(body["item"]["encoding"], "base64")

    def test_fifo_ignores_non_builtin_topics(self):
        port = free_port()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with running_server(
                port=port,
                extra_env={"RCLIPBOARD_FIFO_DIR": str(root)},
            ):
                wait_for_path(root / "health.json")

                status, body = post_json(
                    f"http://127.0.0.1:{port}/v1/clip.put",
                    {
                        "items": [
                            {
                                "topic": "custom",
                                "mime": "text/plain",
                                "encoding": "utf-8",
                                "value": "from-http-custom",
                            }
                        ],
                        "meta": {"app": "http-test"},
                    },
                )
                self.assertEqual(status, 200)
                self.assertEqual(body["items"][0]["topic"], "custom")

                self.assertFalse((root / "put.custom.fifo").exists())
                self.assertFalse((root / "put.custom.fifo.json").exists())
                self.assertFalse((root / "state.custom").exists())
                self.assertFalse((root / "state.custom.json").exists())


if __name__ == "__main__":
    unittest.main()
