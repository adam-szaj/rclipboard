# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup
make install          # create .venv and install deps via uv

# Run server
make run              # TCP on 127.0.0.1:8989
make run-dev          # TCP with --reload + FIFO at runtime.d/
make run-uds          # Unix Domain Socket
make run-proxy        # proxy mode (connects to upstream)
make run-proxy-dev    # proxy mode with --reload on port 7878

# Tests
make test                    # all tests (functional + integration)
make test-http               # HTTP functional tests only
make test-ws                 # WebSocket functional tests only
make test-proxy-integration  # proxy integration tests only

# Run a single test module/case
PYTHONPATH=src .venv/bin/python -m unittest tests.test_functional_http.ClassName.test_method -v

# Linting (ruff, line-length 100)
.venv/bin/ruff check src/

# Quick smoke test (server must be running)
make smoke
make proxy-smoke
```

## Architecture

**rclipboard** is a multi-transport clipboard/message-bus server. A single `AppState` object is the sole owner of topic data; all transports read/write through it.

### Transport stack

| Transport | Entry point | Notes |
|-----------|-------------|-------|
| HTTP REST | `http.py` | `POST /v1/clip.*`, `GET /v1/health.get`, etc. |
| WebSocket  | `ws.py` | JSON-RPC 2.0 at `/ws` |
| FIFO files | `fifo.py` | standalone (`fifo:///path`) or parallel alongside HTTP/WS |
| X11 clipboard | `xsel.py` | polls `xsel` binary, enabled via `RCLIPBOARD_XSEL=1` |
| Proxy (upstream client) | `proxy.py` | opens a WS to an upstream server and replicates topics locally |

All transports share the same Pydantic models from `types.py` (`ClipboardItem`, `TopicData`, JSON-RPC envelopes). HTTP strips the JSON-RPC wrapper (endpoint = method, body = params/result/error); WS uses the full envelope.

### State & concurrency

`AppState` (`app_state.py`) contains:
- `topic_content: dict[str, TopicData]` — stored topics
- `subs: dict[str, set[Interface]]` — per-topic subscriber sets
- `Bus` — an `asyncio.Queue` of `SerialCall` items (discriminated union of `GetTopicData` / `SetTopicData`)
- A single dispatcher task that drains the queue sequentially, eliminating the need for locks

Every write triggers debounced notifications (default 250 ms, `RCLIPBOARD_NOTIFY_DELAY_MS`). `clip.get` flushes pending notifications before returning to guarantee consistency.

### Interface hierarchy

- `Interface` — read-only participant (name, repr)
- `BidirectionalInterface(Interface)` — can receive `clip.changed` events (WS connections, proxy, xsel agent)

Subscriptions use source-filtering so the originating client does not receive its own echo.

### Proxy mode

When `RCLIPBOARD_PROXY=1`, `proxy.py` opens a WebSocket to the upstream, calls `clip.watch` on default topics (`["c", "p", "s"]`), and:
- forwards local `clip.put` upstream
- ingests `clip.changed` from upstream and stores it locally

The local HTTP/WS server then serves local clients, reducing SSH round-trips.

### FIFO dual mode

- Standalone: `RCLIPBOARD_ENDPOINT=fifo:///path` — FIFO is the only transport
- Parallel: set `RCLIPBOARD_FIFO_DIR=/some/dir` alongside a TCP/UDS server — FIFO and HTTP/WS both run

### Key environment variables

| Variable | Purpose |
|----------|---------|
| `RCLIPBOARD_ENDPOINT` | Bind address (TCP `host:port`, UDS path, HTTPS, or `fifo://`) |
| `RCLIPBOARD_PROXY` | `1` to enable proxy mode |
| `RCLIPBOARD_UPSTREAM_ADDR/PORT` | Upstream server for proxy |
| `RCLIPBOARD_FIFO_DIR` | Enable parallel FIFO interface |
| `RCLIPBOARD_XSEL` | `1` to enable X11 clipboard polling |
| `RCLIPBOARD_NOTIFY_DELAY_MS` | Notification debounce delay (default 250) |
| `RCLIPBOARD_LOG_LEVEL` | App log level |
| `RCLIPBOARD_SSL_CERTFILE/KEYFILE` | Paths to TLS cert/key for HTTPS |

### Startup sequence (`main.py` lifespan)

1. Parse config from env vars
2. Create `AppState`
3. Start module tasks in order: proxy → xsel → FIFO → HTTP/WS server
4. On shutdown: cancel tasks in reverse order

### CLI (`scripts/rclipctl`)

Shell script. Auto-detects transport priority: FIFO → UDS → TCP. Subcommands: `clip`, `getclip`, `topics`, `status`, `health`.

### API contract

See `docs/api-contract.md` for the full JSON-RPC 2.0 method specs (`clip.put`, `clip.get`, `clip.watch`, `clip.unwatch`, `topics.list`, `health.get`, `status.get`) and the `ClipboardItem` schema.
