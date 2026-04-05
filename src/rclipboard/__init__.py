import argparse
import os
import sys
from pathlib import Path

import uvicorn

from rclipboard.helpers import bind_endpoint_from_env
from rclipboard.main import create_app


def _run_config_cmd(argv: list[str]) -> None:
    from rclipboard.config import print_env

    parser = argparse.ArgumentParser(prog="rclipboard config")
    subparsers = parser.add_subparsers(dest="cmd")

    env_p = subparsers.add_parser("env", help="print env vars from config.toml")
    env_p.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="FILE",
        help="path to config.toml (default: ~/.config/rclipboard/config.toml)",
    )

    args = parser.parse_args(argv)
    if args.cmd == "env":
        print_env(args.config)
    else:
        parser.print_help()
        sys.exit(1)


def main() -> None:
    # Intercept "config" subcommand before normal server startup.
    if len(sys.argv) >= 2 and sys.argv[1] == "config":
        _run_config_cmd(sys.argv[2:])
        return

    # Apply TOML config before reading env vars — env vars take precedence.
    from rclipboard.config import apply_config
    apply_config()

    parser = argparse.ArgumentParser(prog="rclipboard")
    parser.add_argument("--fd", type=int, default=None)
    args = parser.parse_args()

    endpoint = bind_endpoint_from_env()
    ssl_certfile = os.environ.get("RCLIPBOARD_SSL_CERTFILE")
    ssl_keyfile = os.environ.get("RCLIPBOARD_SSL_KEYFILE")
    ssl_keyfile_password = os.environ.get("RCLIPBOARD_SSL_KEYFILE_PASSWORD")
    if endpoint.scheme in {"ws", "wss"}:
        raise SystemExit("websocket endpoints are not valid bind endpoints for the rclipboard server")
    if endpoint.scheme == "https" and (not ssl_certfile or not ssl_keyfile):
        raise SystemExit(
            "https endpoint requires RCLIPBOARD_SSL_CERTFILE and RCLIPBOARD_SSL_KEYFILE"
        )

    config = uvicorn.Config(
        app="rclipboard.main:app",
        host="" if endpoint.scheme == "uds" or args.fd is not None else endpoint.host or "127.0.0.1",
        port=0 if endpoint.scheme == "uds" or args.fd is not None else int(endpoint.port or 0),
        uds=endpoint.path if endpoint.scheme == "uds" else None,
        fd=args.fd,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
        ssl_keyfile_password=ssl_keyfile_password,
        log_level=os.environ.get("RCLIPBOARD_LOG_LEVEL", "info"),
        reload=os.environ.get("RCLIPBOARD_RELOAD", "0") in {"1", "true", "True"},
    )

    server = uvicorn.Server(config)
    server.run()
