"""rclipctl get: decryption is opt-in via --decrypt (default = no decrypt)."""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT_DIR, free_port, post_json, running_server

ADMIN_TOKEN = "get-decrypt-token"


@unittest.skipUnless(shutil.which("age") and shutil.which("age-keygen"),
                     "age CLI required")
class RclipctlGetDecryptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.port = free_port()
        self.home = tempfile.TemporaryDirectory()
        self.cfg = Path(self.home.name) / ".config" / "rclipboard"
        self.cfg.mkdir(parents=True)
        # generate a keypair in the fake HOME
        subprocess.run(
            [str(ROOT_DIR / "scripts" / "bin" / "rclipctl"), "keygen"],
            env=self._env(), cwd=ROOT_DIR, check=True,
            capture_output=True, text=True,
        )
        self.pub = (self.cfg / "age_key.pub").read_text().strip()
        self.ctx = running_server(
            port=self.port,
            extra_env={"RCLIPBOARD_ADMIN_TOKEN": ADMIN_TOKEN},
        )
        self.ctx.__enter__()
        # register our key so clip.get is allowed to return the ciphertext
        post_json(
            f"http://127.0.0.1:{self.port}/v1/keys.publish",
            {"public_key": self.pub, "label": "t"},
            extra_headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )

    def tearDown(self) -> None:
        self.ctx.__exit__(None, None, None)
        self.home.cleanup()

    def _env(self) -> dict:
        # Clean env — do NOT inherit os.environ: the user's live session sets
        # RCLIP_CONF / RCLIPCTL_TRANSPORT (via /run/user/*/rclipboard/env) which
        # would redirect rclipctl to the user's own UDS server. Keep only what
        # rclipctl needs (PATH for age/curl/jq, HOME/XDG for the fake keypair).
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": self.home.name,
            "XDG_CONFIG_HOME": str(self.cfg.parent),
        }

    def _put_encrypted(self, plaintext: str) -> str:
        # encrypt to our own pubkey via `age`, base64 the armored-less binary
        enc = subprocess.run(
            ["age", "-r", self.pub],
            input=plaintext.encode(), capture_output=True, check=True,
        ).stdout
        cipher_b64 = base64.b64encode(enc).decode()
        post_json(
            f"http://127.0.0.1:{self.port}/v1/clip.put",
            {"items": [{"topic": "c", "mime": "application/octet-stream",
                        "encoding": "base64", "value": cipher_b64,
                        "encrypted": True}], "meta": {}},
        )
        return cipher_b64

    def _get_bytes(self, *args: str) -> bytes:
        # Force TCP to the test server via CLI flags (highest priority), so no
        # inherited config/env file can redirect the transport. Use --host/--port
        # rather than --endpoint (the latter is currently overridden by the
        # default HOST/PORT under CLI_CONFIGURED — a separate rclipctl bug).
        # Bytes (not text): without --decrypt the stored ciphertext decodes to
        # raw binary age output, which is not valid UTF-8.
        return subprocess.run(
            [str(ROOT_DIR / "scripts" / "bin" / "rclipctl"), "get", "-t", "c",
             "--host", "127.0.0.1", "--port", str(self.port),
             "--transport", "tcp", *args],
            env=self._env(), cwd=ROOT_DIR,
            capture_output=True, check=True,
        ).stdout

    def test_get_without_decrypt_does_not_reveal_plaintext(self):
        # Stored ciphertext must NOT decrypt without --decrypt. The value comes
        # back as the raw (base64-decoded) age blob, which starts with the age
        # header and never contains the plaintext.
        self._put_encrypted("s3cret")
        out = self._get_bytes()  # no --decrypt
        self.assertNotIn(b"s3cret", out)
        self.assertIn(b"age-encryption.org", out)

    def test_get_with_decrypt_returns_plaintext(self):
        self._put_encrypted("s3cret")
        out = self._get_bytes("--decrypt")
        self.assertEqual(out, b"s3cret")


if __name__ == "__main__":
    unittest.main()
