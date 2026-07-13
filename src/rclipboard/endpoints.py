"""Endpoint parsing and env-based bind/upstream endpoint resolution."""
import os
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class EndpointConfig:
    scheme: str
    host: str | None = None
    port: int | None = None
    path: str | None = None


def parse_endpoint(
    raw: str,
    *,
    default_host: str = "127.0.0.1",
    default_port: int = 8989,
) -> EndpointConfig:
    raw = raw.strip()
    if not raw:
        raise ValueError("endpoint must not be empty")

    if "://" in raw:
        parsed = urlparse(raw)
        scheme = parsed.scheme
        if scheme in {"http", "https", "ws", "wss"}:
            return EndpointConfig(
                scheme=scheme,
                host=parsed.hostname or default_host,
                port=parsed.port or default_port,
            )
        if scheme == "uds":
            path = parsed.path or parsed.netloc
            if not path:
                raise ValueError(f"{scheme} endpoint requires a path")
            return EndpointConfig(scheme=scheme, path=path)
        raise ValueError(f"unsupported endpoint scheme: {scheme}")

    if raw.startswith("/"):
        return EndpointConfig(scheme="uds", path=raw)

    if raw.isdigit():
        return EndpointConfig(scheme="http", host=default_host, port=int(raw))

    if ":" in raw:
        host, port_raw = raw.rsplit(":", 1)
        if not port_raw.isdigit():
            raise ValueError(f"invalid endpoint port: {raw}")
        return EndpointConfig(scheme="http",
                              host=host or default_host,
                              port=int(port_raw))

    return EndpointConfig(scheme="http", host=raw, port=default_port)


def bind_endpoint_from_env() -> EndpointConfig:
    endpoint = os.environ.get("RCLIPBOARD_ENDPOINT")
    if endpoint:
        return parse_endpoint(endpoint)

    uds = os.environ.get("RCLIPBOARD_BIND_UDS")
    if uds:
        return EndpointConfig(scheme="uds", path=uds)

    host = os.environ.get("RCLIPBOARD_BIND_ADDR", "127.0.0.1")
    port = int(os.environ.get("RCLIPBOARD_BIND_PORT", 8989))
    return EndpointConfig(scheme="http", host=host, port=port)


def upstream_endpoint_from_env() -> EndpointConfig:
    host = os.environ.get("RCLIPBOARD_UPSTREAM_ADDR", "127.0.0.1")
    port = int(os.environ.get("RCLIPBOARD_UPSTREAM_PORT", 8989))
    uds = os.environ.get("RCLIPBOARD_UPSTREAM_UDS")
    endpoint = os.environ.get("RCLIPBOARD_UPSTREAM_ENDPOINT")
    if endpoint:
        return parse_endpoint(endpoint)

    if uds:
        return EndpointConfig(scheme="uds", path=uds, host=host, port=port)

    return EndpointConfig(scheme="http", host=host, port=port)
