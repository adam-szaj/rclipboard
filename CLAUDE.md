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
make test                    # all tests (functional + integration + ssl)
make test-http               # HTTP functional tests only
make test-ws                 # WebSocket functional tests only
make test-proxy-integration  # proxy integration tests only
make test-ssl                # HTTPS/WSS + SSL proxy tests

# Key registry tests
PYTHONPATH=src .venv/bin/python -m unittest tests.test_functional_keys -v

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

### Conflict resolution (time-based sync)

Both the main server and a proxy may already hold buffered topics in memory when they (re)connect. To merge them deterministically, every stored item carries a UTC timestamp (`meta["ts"]`, stamped on `clip.put` if absent) and resolution follows **"newer wins; on a tie the local value wins"**:

- **Central decision** — `AppState._process_data_item` calls `_accept_incoming()` before overwriting `topic_content[topic]`. This protects *every* ingest path (proxy, ws, uds, xsel, fifo, http) uniformly. Rejected items are flagged `rejected=True`; `process_put_item` then skips monitoring updates and notifications.
- **Clock-offset exchange** — peers' wall clocks may differ, so absolute `ts` values aren't directly comparable. During the `clip.watch` handshake the watcher sends `peer_now_utc` and the server replies with `server_now_utc`. The proxy estimates the offset with Cristian's algorithm (midpoint of send/receive vs. server time) and **normalises** each upstream item's `ts` into the local clock (`ProxyClient._normalize_peer_ts`) before storing it. Symmetrically, the server records the watcher's clock (`RPCHandler._record_peer_clock`) and normalises the timestamps on items that peer puts (`_handle_clip_put`).
- **Tie window** — the offset estimate has residual error (~RTT/2) and clocks drift, so timestamps within `RCLIPBOARD_SYNC_TIE_MS` (default 100 ms) are treated as a tie. On a tie a **local** value supersedes a **remote** one (a proxy-ingested item is "remote", detected via the `is_remote_source` marker on `ProxyClient`). 100 ms is well above localhost RTT/2 yet far below the gap between two human clipboard actions, so genuine updates are never masked.
- **Backwards compatible** — if either side lacks a comparable `ts` (legacy item), the incoming value is accepted as before.

`InternalTopicData.compare_ts` holds the (normalised) timestamp used for the comparison; `enqueue_topic_data[_nowait]` accepts an optional `compare_ts` so ingest paths can supply the clock-corrected value.

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
| `RCLIPBOARD_RAW_UDS_PATH` | Unix Domain Socket path for JSON-RPC 2.0 NDJSON transport (default: `/tmp/clipboard_raw.sock`) |
| `RCLIPBOARD_PROXY` | `1` to enable proxy mode |
| `RCLIPBOARD_UPSTREAM_ENDPOINT` | Upstream server endpoint for proxy (TCP `host:port`, UDS, or WS URL) |
| `RCLIPBOARD_XSEL` | `1` to enable X11 clipboard polling |
| `RCLIPBOARD_XSEL_PATH` | Path to `xsel` binary (default: `/usr/bin/xsel`) |
| `RCLIPBOARD_XSEL_INTERVAL_MS` | X11 clipboard poll interval (default: 500) |
| `RCLIPBOARD_XSEL_ENCRYPT` | `1` to encrypt X11 clipboard data via `rclipctl exec` (default: `0`) |
| `RCLIPBOARD_DISPLAY_ENV_FILE` | File holding `DISPLAY`/`WAYLAND_DISPLAY`/`XAUTHORITY` for the current graphical session, re-read by xsel each call (default: `${XDG_RUNTIME_DIR}/rclipboard/display.env`) |
| `RCLIPCTL_PATH` | Path to `rclipctl` binary used by xsel encrypt mode (default: `rclipctl`) |
| `RCLIPBOARD_NOTIFY_DELAY_MS` | Notification debounce delay (default: 250) |
| `RCLIPBOARD_SYNC_TIE_MS` | Tie window (ms) for time-based conflict resolution; timestamps within it are a tie → local wins (default: 100) |
| `RCLIPBOARD_LOG_LEVEL` | App log level |
| `RCLIPBOARD_PY_LOG_LEVEL` | Python logging level override |
| `RCLIPBOARD_SSL_CERTFILE/KEYFILE` | Paths to TLS cert/key for HTTPS |
| `RCLIPBOARD_SSL_KEYFILE_PASSWORD` | Password for encrypted private key |
| `RCLIPBOARD_RELOAD` | `1` to enable uvicorn reload mode (dev only) |
| `RCLIPBOARD_ADMIN_TOKEN` | Bearer token required for `POST /v1/keys.publish` (empty = registry disabled) |
| `RCLIPBOARD_AGE_KEY_FILE` | Path to age private key file (default: `~/.config/rclipboard/age_key.txt`) |
| `RCLIPBOARD_KNOWN_KEYS_FILE` | Path to file with recipient public keys (default: `~/.config/rclipboard/known_keys`) |

### Configuration via config.toml

Configuration can be loaded from `~/.config/rclipboard/config.toml` (or path specified by `RCLIPBOARD_CONFIG`). The config file supports variable expansion via `${VAR}` or `$VAR` syntax (e.g., `${XDG_RUNTIME_DIR}`).

Priority order (highest wins): CLI flags > environment variables > config.toml > built-in defaults.

**Example config.toml** (all sections are optional):

```toml
[server]
# Bind address: "127.0.0.1:8989" (TCP), "/tmp/clipboard.sock" (UDS), "0.0.0.0:8989" (TCP any), "wss://localhost:8989" (HTTPS)
endpoint = "127.0.0.1:8989"

# Raw UDS socket (JSON-RPC 2.0 NDJSON transport)
raw_uds_path = "/tmp/clipboard_raw.sock"

# Log levels: trace, debug, info, warn, error
log_level = "info"
py_log_level = "INFO"

# Debounce delay for clip.changed notifications (milliseconds)
notify_delay_ms = 250

# Development: enable uvicorn auto-reload on file changes
reload = false

# Admin token for POST /v1/keys.publish; leave empty to disable key registry
admin_token = ""

[xsel]
# X11 clipboard integration (Linux with X11 server)
enabled = true
path = "/usr/bin/xsel"
interval_ms = 500
# Encrypt X11 clipboard data via rclipctl exec (requires age + registered keys)
encrypt = false

[proxy]
# Enable proxy mode (upstream client)
enabled = false

# Upstream server: "host:port" (TCP), "/path/to/socket" (UDS), "wss://host:port" (WSS)
upstream_endpoint = "localhost:8989"

[ssl]
# TLS certificates for HTTPS/WSS
certfile = ""
keyfile = ""
keyfile_password = ""  # optional, for encrypted private keys

[client]
# rclipctl configuration (not server settings)
# Transport priority: "fifo", "uds", "tcp"
transport = ""
endpoint = ""

[encryption]
# Path to age private key (used by rclipctl for decryption)
key_file = "~/.config/rclipboard/age_key.txt"
# File with one recipient public key per line (used by rclipctl --encrypt)
known_keys_file = "~/.config/rclipboard/known_keys"
```

**Loading configuration:**

```bash
# Apply config.toml and start server
rclipboard config env | xargs -0 env
make run

# Or directly:
RCLIPBOARD_CONFIG=~/.config/rclipboard/config.toml make run
```

The `rclipboard config env` command prints all resolved environment variables (after expansion) as `KEY="value"` lines, suitable for:
- Shell sourcing: `eval "$(rclipboard config env)"`
- Systemd `EnvironmentFile=` directive
- Manual export to `~/.config/rclipboard/env`

### Startup sequence (`main.py` lifespan)

1. Load and apply `config.toml` settings
2. Create `AppState` (starts dispatcher task)
3. Install and register modules in order: proxy → xsel → raw UDS → HTTP/WS server
   - **Proxy**: `ProxyClient` connects upstream, replicates topics via `clip.watch`, registers as subscriber
   - **Xsel**: `XselInterface` polls X11 clipboard, registers as subscriber
   - **Raw UDS** (`/tmp/clipboard_raw.sock`): accepts connections, per-connection subscriber with drainer
   - **HTTP/WS**: FastAPI/uvicorn handles TCP/UDS endpoint (HTTP REST + WebSocket at `/ws`)
   - Each module registers as a `BidirectionalInterface` subscriber
   - Drainer tasks are started automatically via `start_drainer()` in `register_client()`
4. On shutdown:
   - Cancel background tasks (fire-and-forget tasks tracked in `AppState._background_tasks`)
   - Flush pending notifications
   - Use `TaskGroup` to shut down proxy/xsel/raw UDS modules in parallel
   - Each shutdown calls `stop_drainer()` before unregister to drain queued updates
   - Cancel the dispatcher task last
   - HTTP/WS server shuts down implicitly when lifespan exits

### CLI (`scripts/bin/rclipctl`)

Shell script. Auto-detects transport priority: FIFO → UDS → TCP.

| Subcommand | Purpose |
|-----------|---------|
| `put` / `clip` | Read stdin and call `clip.put` |
| `get` | Call `clip.get`, auto-decrypt if `encrypted=true` |
| `exec --encrypt-output -- <cmd>` | Run command, encrypt stdout with age, print ciphertext to stdout |
| `exec --decrypt-input -- <cmd>` | Decrypt stdin with local age key, pipe plaintext to command |
| `keygen` | Generate age X25519 keypair (`age_key.txt` + `age_key.pub`) |
| `register [--token T]` | Register own public key with server (`POST /v1/keys.publish`) |
| `keys-list` | List registered public keys (`GET /v1/keys.list`) |
| `status` / `topics` / `health` | Server info |

Encryption flags on `put`: `--encrypt` / `-E`, `--key <age1...>`, `--key-file <path>`, `--fetch-keys` (fetch from server registry).

`exec` exits before transport resolution — it is a pure stdio pipe with no server communication.

### Module responsibilities

| Module | Responsibility |
|--------|-----------------|
| `app_state.py` | Central topic storage, subscriber management, dispatch queue, debounce logic, background task tracking, in-memory public key registry |
| `types.py` | Pydantic models, `Interface`/`BidirectionalInterface` base classes with drainer infrastructure |
| `http.py` | HTTP REST endpoints; `POST /v1/keys.publish` (token auth), `GET /v1/keys.list`, `clip.get` encrypted gate |
| `ws.py` | WebSocket server, JSON-RPC 2.0 handling, per-connection drainer |
| `uds.py` | Raw Unix Domain Socket server, JSON-RPC 2.0 over NDJSON, per-connection drainer |
| `xsel.py` | X11 clipboard poller; optional encrypt mode via `rclipctl exec` wrapper |
| `proxy.py` | Upstream WebSocket client, watch subscription, local topic replication |
| `config.py` | Loads `config.toml`, maps TOML fields to env vars, supports `${VAR}` expansion |

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

### Encryption (client-side, age format)

The server is a **blind store** — it never encrypts or decrypts data. All encryption is client-side using the [age](https://age-encryption.org/) format via the `age` CLI.

**`ClipboardItem.encrypted`** — bool field (default `false`). When `true`:
- `value` contains base64-encoded age ciphertext
- `clip.get` over HTTP requires `X-Age-Public-Key: age1...` header with a key registered in the server's in-memory registry; returns 403 if missing or unknown
- `rclipctl get` auto-decrypts using the local private key

**Public key registry** (`AppState.public_keys`):
- `POST /v1/keys.publish` — register a public key; requires `Authorization: Bearer <RCLIPBOARD_ADMIN_TOKEN>`; returns 503 if token not configured
- `GET /v1/keys.list` — open; returns all registered keys
- Registry is in-memory only (lost on restart)

**`rclipctl` encryption workflow:**
```bash
rclipctl keygen                          # generate keypair
rclipctl register --token <admin_token>  # register public key with server
echo "secret" | rclipctl put --encrypt --fetch-keys  # encrypt + send
rclipctl get                             # auto-decrypt
```

**`rclipctl exec` — pure stdio encryption pipe** (no server communication):
```bash
# Encrypt a command's stdout and print ciphertext to stdout
rclipctl exec --encrypt-output --key age1... -- xsel -ob

# Decrypt stdin and feed plaintext to a command's stdin
rclipctl exec --decrypt-input -- xsel -ib
```

**xsel encrypted mode** (`RCLIPBOARD_XSEL_ENCRYPT=1`, default off):
- Read: calls `rclipctl exec --encrypt-output --fetch-keys -- xsel <opt> -o`, stores ciphertext with `meta["encrypted"]=true`
- Write: if incoming data is encrypted, calls `rclipctl exec --decrypt-input -- xsel -n -i <opt>`
- Requires `age`, `rclipctl`, and at least one registered public key on PATH

**Display env-file (DISPLAY race fix)**: When `rclipboard.service` starts at login it
may not yet have `DISPLAY` (GNOME/Wayland import it into the systemd user
environment only after the graphical session is up), so xsel silently no-ops.
Rather than restart the server (which would lose in-memory clipboard state),
xsel re-reads `DISPLAY`/`WAYLAND_DISPLAY`/`XAUTHORITY` from a small env-file on
*every* invocation (`load_display_env()` in `xsel.py`, falling back to the
process env). The session-bound `rclipboard-display.service`
(`WantedBy=graphical-session.target`, `PartOf=graphical-session.target`) writes
that file from the live session and removes it on `ExecStop`. Because the unit
is per-user and tied to *this* user's graphical session, a different user
logging in at the console never leaks their `DISPLAY` into this server. The main
`rclipboard.service` stays fully transparent — it never needs `DISPLAY`.

### API contract

See `docs/api-contract.md` for the full JSON-RPC 2.0 method specs (`clip.put`, `clip.get`, `clip.watch`, `clip.unwatch`, `topics.list`, `health.get`, `status.get`) and the `ClipboardItem` schema. All methods are available on HTTP, WebSocket, and raw UDS transports (with HTTP using REST conventions).

Additional HTTP-only endpoints: `POST /v1/keys.publish`, `GET /v1/keys.list`.


