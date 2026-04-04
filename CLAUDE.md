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
| Raw UDS | `uds.py` | JSON-RPC 2.0 over NDJSON, low-overhead alternative to HTTP/WS |
| X11 clipboard | `xsel.py` | polls `xsel` binary, enabled via `RCLIPBOARD_XSEL=1` |
| Proxy (upstream client) | `proxy.py` | opens a WS to an upstream server and replicates topics locally |

All transports share the same Pydantic models from `types.py` (`ClipboardItem`, `TopicData`, JSON-RPC envelopes). HTTP strips the JSON-RPC wrapper (endpoint = method, body = params/result/error); WS and raw UDS use the full JSON-RPC 2.0 envelope with identical message format.

### State & concurrency

`AppState` (`app_state.py`) contains:
- `topic_content: dict[str, TopicData]` — stored topics
- `subs: dict[str, set[Interface]]` — per-topic subscriber sets
- `Bus` — an `asyncio.Queue` of `SerialCall` items (discriminated union of `GetTopicData` / `SetTopicData`)
- A single dispatcher task that drains the queue sequentially, eliminating the need for locks
- `_background_tasks: set` — tracks fire-and-forget tasks for lifecycle management

Every write triggers debounced notifications (default 250 ms, `RCLIPBOARD_NOTIFY_DELAY_MS`). `clip.get` flushes pending notifications before returning to guarantee consistency.

The dispatcher calls `deliver()` (sync) on subscribers rather than awaiting `send()` directly. This decouples slow subscribers from the dispatcher loop.

### Interface hierarchy and drainer pattern

- `Interface` — read-only participant (name, repr)
- `BidirectionalInterface(Interface)` — can receive `clip.changed` events via `send()` method
  - Has drainer infrastructure: `_pending` dict per topic, `_pending_event`, `_drainer_task`
  - `deliver(data)` — sync method that sets `_pending[data.topic] = data` and signals event
  - `_run_drainer()` — independent async task that waits for signal, batches latest updates per topic, then calls `send()` for each
  - Each subscriber processes its own queue independently, preventing slow connections (e.g., proxy upstream) from blocking others
  - Latest-value-only semantics: dict overwrites guarantee only final value per topic is delivered even with rapid updates
  - `start_drainer()` / `stop_drainer()` — lifecycle management called by register/unregister and shutdown

All transports (WS connections, proxy, xsel, FIFO) extend `BidirectionalInterface` and run drainers. Subscriptions use source-filtering so the originating client does not receive its own echo.

### Proxy mode

When `RCLIPBOARD_PROXY=1`, `proxy.py` instantiates a `ProxyClient` (extends `BidirectionalInterface`) that:
- opens a WebSocket to the upstream server
- calls `clip.watch` on default topics (`["c", "p", "s"]`) to subscribe
- forwards local `clip.put` upstream via `send()`
- ingests `clip.changed` from upstream and stores it locally via `app_state.enqueue_topic_data()`

The local HTTP/WS server then serves local clients, reducing SSH round-trips. The proxy is a regular subscriber registered in `app_state`, so it receives all local topic updates through its drainer (decoupled from dispatcher). Upstream sends are non-blocking and buffered per-topic.

### FIFO dual mode

- Standalone: `RCLIPBOARD_ENDPOINT=fifo:///path` — FIFO is the only transport
- Parallel: set `RCLIPBOARD_FIFO_DIR=/some/dir` alongside a TCP/UDS server — FIFO and HTTP/WS both run

`FIFOTransport` extends `BidirectionalInterface` and manages:
- Reader tasks for raw and JSON put FIFOs (`put.{topic}.fifo`, `put.{topic}.fifo.json`) that enqueue incoming data
- `send()` override that writes state snapshots atomically (raw + JSON) to `state.{topic}` and `state.{topic}.json`
- In read-write mode (`RCLIPBOARD_FIFO_MODE=rw`), debounced snapshot updates (topics, health, status) via `refresh_snapshots()`
- Atomic writes via `tempfile.mkstemp()` + `os.replace()` to avoid partial-write races

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
2. Create `AppState` (starts dispatcher task)
3. Install and register modules in order: proxy → xsel → FIFO → HTTP/WS server
   - Each module (proxy, xsel, FIFO) registers as a `BidirectionalInterface` subscriber
   - drainer tasks are started automatically via `start_drainer()` in `register_client()`
4. On shutdown:
   - Cancel background tasks (fire-and-forget tasks tracked in `AppState._background_tasks`)
   - Flush pending notifications
   - Use `TaskGroup` to shut down proxy/xsel/FIFO modules in parallel
   - Each shutdown calls `stop_drainer()` before unregister to drain queued updates
   - Cancel the dispatcher task last
   - HTTP/WS server shuts down implicitly when lifespan exits

### CLI (`scripts/rclipctl`)

Shell script. Auto-detects transport priority: FIFO → UDS → TCP. Subcommands: `clip`, `getclip`, `topics`, `status`, `health`.

### Module responsibilities

| Module | Responsibility |
|--------|-----------------|
| `app_state.py` | Central topic storage, subscriber management, dispatch queue, debounce logic, background task tracking |
| `types.py` | Pydantic models, `Interface`/`BidirectionalInterface` base classes with drainer infrastructure |
| `http.py` | HTTP REST endpoints (blocking by design, no async send) |
| `ws.py` | WebSocket server, JSON-RPC 2.0 handling, per-connection drainer |
| `uds.py` | Raw Unix Domain Socket server, JSON-RPC 2.0 over NDJSON, per-connection drainer |
| `xsel.py` | X11 clipboard poller, async subprocess execution with timeout |
| `proxy.py` | Upstream WebSocket client, watch subscription, local topic replication |

### Task lifecycle and drainer pattern details

**Background tasks**: Fire-and-forget tasks (e.g., FIFO reader loops) are tracked in `AppState._background_tasks` and cancelled on shutdown via `cancel_background_tasks()`.

**Drainer pattern**: Each `BidirectionalInterface` (WS connection, proxy, xsel, FIFO, HTTP) has:
- Sync `deliver(data)` called by dispatcher: sets `_pending[topic] = data`, signals event
- Async `_run_drainer()` loop:
  1. Wait for event
  2. Snapshot and clear `_pending` dict (atomic under lock)
  3. Call async `send()` for each unique topic in batch
- Independent per-subscriber: slow upstream (proxy) doesn't block xsel or FIFO
- Latest-value-only: rapid updates to same topic overwrite in `_pending` before drainer runs

**Shutdown ordering** (via `TaskGroup` for parallelism):
1. Modules notify they're shutting down (set `proxy_enabled=False`, etc.)
2. Each module's `stop_drainer()` cancels the drainer task and awaits final batch
3. Unsubscribe from app_state
4. Main dispatcher task is cancelled last

### Raw UDS transport details

The raw UDS server at `/tmp/clipboard_raw.sock` (path configurable via `RCLIPBOARD_RAW_UDS_PATH`) provides:
- Same JSON-RPC 2.0 message format as WebSocket (`/ws`)
- One-line JSON messages (`\n` terminated) in both directions
- Per-connection drainer: each client gets its own subscriber with independent delivery queue
- Identical request/response handling and notification delivery as WS
- No HTTP overhead or authentication (local/trusted only)
- Use case: shell scripts, editor integrations, embedded clients, minimal dependencies

Message flow: client sends `{"jsonrpc":"2.0","id":123,"method":"clip.get","params":{...}}`, server responds with `{"jsonrpc":"2.0","id":123,"result":{...}}` or `{"jsonrpc":"2.0","id":123,"error":{...}}`. Subscriptions (via `clip.watch`) receive unsolicited `{"jsonrpc":"2.0","method":"clip.changed","params":{...}}` events.

### API contract

See `docs/api-contract.md` for the full JSON-RPC 2.0 method specs (`clip.put`, `clip.get`, `clip.watch`, `clip.unwatch`, `topics.list`, `health.get`, `status.get`) and the `ClipboardItem` schema. All methods are available on HTTP, WebSocket, and raw UDS transports (with HTTP using REST conventions).


