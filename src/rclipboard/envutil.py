"""Environment variable helpers."""
import os

_TRUE = ("1", "true", "on")
_FALSE = ("0", "false", "off")


def env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean env var: true = 1/true/on, false = 0/false/off.

    Case-insensitive. Unset, empty or unrecognised values return ``default``.
    """
    raw = os.environ.get(name, "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return default
