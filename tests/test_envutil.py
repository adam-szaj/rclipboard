import unittest
from unittest import mock

from rclipboard.envutil import env_bool


class EnvBoolTests(unittest.TestCase):
    def _check(self, raw: str | None, expected: bool, default: bool = False):
        env = {} if raw is None else {"X": raw}
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertEqual(env_bool("X", default), expected,
                             f"raw={raw!r} default={default}")

    def test_true_values(self):
        for raw in ("1", "true", "True", "TRUE", "on", "ON", " on "):
            self._check(raw, True)

    def test_false_values(self):
        for raw in ("0", "false", "False", "FALSE", "off", "OFF"):
            self._check(raw, False, default=True)

    def test_unset_and_empty_use_default(self):
        self._check(None, False)
        self._check(None, True, default=True)
        self._check("", True, default=True)

    def test_unrecognised_uses_default(self):
        self._check("yes", False)
        self._check("enabled", True, default=True)


if __name__ == "__main__":
    unittest.main()
