"""TOML configuration loader for rclipboard.

Priority (highest wins):
  CLI flags  >  environment variables  >  config.toml  >  built-in defaults

Usage in server:
    from rclipboard.config import apply_config
    apply_config()          # call before any os.environ.get() reads

Usage for shell scripts:
    rclipboard config env   # prints KEY=VALUE lines, shell/systemd-sourceable
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore[import]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "rclipboard" / "config.toml"


def _bool_str(v: object) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    return str(v)


# (toml_section, toml_key, env_var_name, value_converter)
_FIELDS: list[tuple[str, str, str, object]] = [
    ("server", "endpoint", "RCLIPBOARD_ENDPOINT", str),
    ("server", "log_level", "RCLIPBOARD_LOG_LEVEL", str),
    ("server", "py_log_level", "RCLIPBOARD_PY_LOG_LEVEL", str),
    ("server", "notify_delay_ms", "RCLIPBOARD_NOTIFY_DELAY_MS", str),
    ("server", "sync_tie_ms", "RCLIPBOARD_SYNC_TIE_MS", str),
    ("server", "encrypted_ttl_s", "RCLIPBOARD_ENCRYPTED_TTL_S", str),
    ("server", "graceful_timeout_s", "RCLIPBOARD_GRACEFUL_TIMEOUT_S", str),
    ("xsel", "enabled", "RCLIPBOARD_XSEL", _bool_str),
    ("xsel", "path", "RCLIPBOARD_XSEL_PATH", str),
    ("xsel", "interval_ms", "RCLIPBOARD_XSEL_INTERVAL_MS", str),
    ("xsel", "encrypt", "RCLIPBOARD_XSEL_ENCRYPT", _bool_str),
    ("xsel", "display_env_file", "RCLIPBOARD_DISPLAY_ENV_FILE", str),
    ("pasteboard", "enabled", "RCLIPBOARD_PASTEBOARD", _bool_str),
    ("pasteboard", "pbcopy_path", "RCLIPBOARD_PBCOPY_PATH", str),
    ("pasteboard", "pbpaste_path", "RCLIPBOARD_PBPASTE_PATH", str),
    ("pasteboard", "interval_ms", "RCLIPBOARD_PASTEBOARD_INTERVAL_MS", str),
    ("raw_uds", "path", "RCLIPBOARD_RAW_UDS_PATH", str),
    ("server", "admin_token", "RCLIPBOARD_ADMIN_TOKEN", str),
    ("server", "reload", "RCLIPBOARD_RELOAD", _bool_str),
    ("proxy", "enabled", "RCLIPBOARD_PROXY", _bool_str),
    ("proxy", "upstream_endpoint", "RCLIPBOARD_UPSTREAM_ENDPOINT", str),
    ("proxy", "lazy_upstream_kb", "RCLIPBOARD_LAZY_UPSTREAM_KB", str),
    ("proxy", "lazy_local_kb", "RCLIPBOARD_LAZY_LOCAL_KB", str),
    ("proxy", "upstream_put_mode", "RCLIPBOARD_UPSTREAM_PUT_MODE", str),
    ("proxy", "upstream_sync_delay_ms", "RCLIPBOARD_UPSTREAM_SYNC_DELAY_MS", str),
    ("ssl", "certfile", "RCLIPBOARD_SSL_CERTFILE", str),
    ("ssl", "keyfile", "RCLIPBOARD_SSL_KEYFILE", str),
    ("ssl", "keyfile_password", "RCLIPBOARD_SSL_KEYFILE_PASSWORD", str),
    ("client", "transport", "RCLIPCTL_TRANSPORT", str),
    ("client", "endpoint", "RCLIPCTL_ENDPOINT", str),
    ("encryption", "key_file", "RCLIPBOARD_AGE_KEY_FILE", str),
    ("encryption", "known_keys_file", "RCLIPBOARD_KNOWN_KEYS_FILE", str),
]


def _config_path() -> Path:
    return Path(os.environ.get("RCLIPBOARD_CONFIG", str(DEFAULT_CONFIG_PATH)))


def load_config(path: Path | None = None) -> dict[str, str]:
    """Read config.toml and return a mapping of env-var-name → value string.

    String values containing ``${VAR}`` or ``$VAR`` are expanded via
    :func:`os.path.expandvars` so templates like ``${XDG_RUNTIME_DIR}`` work.
    Empty string values are skipped (treated as "not set").
    """
    if tomllib is None:
        return {}
    resolved = path or _config_path()
    if not resolved.exists():
        return {}
    with resolved.open("rb") as f:
        data = tomllib.load(f)

    result: dict[str, str] = {}
    for section, key, env_var, converter in _FIELDS:
        section_data = data.get(section)
        if not isinstance(section_data, dict):
            continue
        if key not in section_data:
            continue
        raw = section_data[key]
        if raw is None or raw == "":
            continue
        converted: str = converter(raw)  # type: ignore[operator]
        if isinstance(converted, str):
            converted = os.path.expandvars(converted)
        result[env_var] = converted
    return result


def apply_config(path: Path | None = None) -> None:
    """Apply config.toml values to ``os.environ``.

    Only sets variables that are not already present in the environment,
    so explicit env-var overrides always win.
    """
    for env_var, value in load_config(path).items():
        if env_var not in os.environ:
            os.environ[env_var] = value


def print_env(path: Path | None = None) -> None:
    """Print config as ``KEY="value"`` lines.

    Output is suitable for:
    - shell sourcing: ``eval "$(rclipboard config env)"``
    - systemd ``EnvironmentFile=`` (double-quoted values are stripped)
    - writing to ``~/.config/rclipboard/env``

    All values are already expanded (no shell variable references remain).
    """
    for env_var, value in sorted(load_config(path).items()):
        # Escape backslashes and double-quotes; neutralise $ so the file
        # is safe to source in bash without unexpected expansion.
        safe = value.replace("\\", "\\\\").replace('"',
                                                   '\\"').replace("$", "\\$")
        print(f'{env_var}="{safe}"')
