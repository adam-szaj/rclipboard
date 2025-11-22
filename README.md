# RemoteClipboard (rclipboard)

A lightweight clipboard/message bus for local and remote workflows. It keeps the latest value per topic, broadcasts changes to WebSocket subscribers, and exposes simple HTTP endpoints for publishing and fetching data. It integrates with the X clipboard (xsel) and supports a proxy mode for remote synchronization over SSH tunnels or TCP.

Highlights
- HTTP and WebSocket APIs with a consistent message format.
- DataItem payloads support JSON or binary (base64 with padding or lowercase hex).
- X clipboard (xsel) two‑way sync for clipboard/primary/secondary.
- Optional remote proxy client to bridge multiple machines.
- TCP, IPv6, Unix Domain Socket, and HTTPS support.
- Tiny CLI helper for common HTTP operations.

Use Cases
- Sync Linux clipboard(s) across shells, tmux, neovim, and remote hosts.
- Publish binary blobs or small files between processes without setting up a full broker.
- Trigger light notifications via topics while retrieving heavy payloads on demand.

Quick Start (Short)
1) Run server quickly:
   uvicorn app.main:app --host 127.0.0.1 --port 8989
2) Publish text:
   echo -n 'hello' | ./rclipctl publish -c --encoding base64 --json | jq .
3) Fetch last value:
   ./rclipctl fetch -c --json | jq .
4) Health:
   ./rclipctl health --host 127.0.0.1 --port 8989 | jq .
   # or with curl
   curl -fsS http://127.0.0.1:8989/health | jq .

Quick Start
1) Install deps (Python 3.11+):
   python -m pip install fastapi uvicorn websockets

2) Run the server:
   uvicorn app.main:app --host 127.0.0.1 --port 8989

3) Publish (HTTP, JSON envelope):
   echo -n 'hello' | ./rclipctl publish -c --encoding base64 --json --host 127.0.0.1 --port 8989 | jq .
   # or with curl
   curl -fsS -X POST \
     'http://127.0.0.1:8989/publish/c?response-type=json' \
     -H 'Content-Type: application/json' \
     -d '{"meta":{"app":"curl"},"data":{"topic":"c","value":"aGVsbG8","valueType":"binary","valueEncoding":"base64"}}' | jq .

4) Fetch (HTTP, envelope):
   ./rclipctl fetch -c --json --host 127.0.0.1 --port 8989 | jq .
   # or with curl
   curl -fsS 'http://127.0.0.1:8989/fetch/c?response-type=json' | jq .

5) Subscribe (WS): connect to ws://127.0.0.1:8989/ws and:
   {"type":"system-request","id":1,"action":"subscribe","topics":["c"]}
   Receive broadcasts on changes.

Protocol (Summary)
- DataItem
  - topic: string
  - value: any
  - valueType?: string (string|number|boolean|object|array|binary)
  - valueEncoding?: string (base64 with padding | hex lowercase) — required if valueType==binary
- HTTP
  - POST /publish/{topic}: body {meta?, data: DataItem|DataItem[]} → 202 or Response.return when ?response-type=json
  - GET /fetch/{topic}: 200 empty or Response.return when ?response-type=json
  - GET /topics, GET /status, GET /health
- WebSocket
  - Client → Server: system-request (subscribe|unsubscribe|ping); request/call (method: publish|get)
  - Server → Client: system-response (subscribed|unsubscribed|pong), response.return/error, broadcast.publish

HTTP Examples
- Status:
  ./rclipctl status --host 127.0.0.1 --port 8989
  # or: curl -fsS http://127.0.0.1:8989/status | jq .
- Topics:
  ./rclipctl topics --host 127.0.0.1 --port 8989
  # or: curl -fsS http://127.0.0.1:8989/topics | jq .
- Publish (status-only mode):
  echo -n 'hello' | ./rclipctl publish -c --encoding hex --host 127.0.0.1 --port 8989
  # or: curl -fsS -X POST 'http://127.0.0.1:8989/publish/c' -H 'Content-Type: application/json' \
  #       -d '{"meta":{"app":"curl"},"data":{"topic":"c","value":"68656c6c6f","valueType":"binary","valueEncoding":"hex"}}'
- Fetch (status-only mode; non‑JSON prints status line):
  ./rclipctl fetch -c --host 127.0.0.1 --port 8989
  # or: curl -i -s 'http://127.0.0.1:8989/fetch/c' | head -n 1
- Health:
  ./rclipctl health --host 127.0.0.1 --port 8989 | jq .
  # or: curl -fsS http://127.0.0.1:8989/health | jq .

WebSocket Examples
- Subscribe and publish:
  # subscribe
  {"type":"system-request","id":1,"action":"subscribe","topics":["c"]}
  # publish
  {"type":"request","id":2,"action":"call","method":"publish","params":{"data":{"topic":"c","value":"aGVsbG8","valueType":"binary","valueEncoding":"base64"}},"meta":{"app":"demo"}}
  # broadcast (from server)
  {"type":"broadcast","action":"publish","data":{"topic":"c","value":"aGVsbG8","valueType":"binary","valueEncoding":"base64"},"meta":{"app":"demo"},"ts":"..."}

CLI Helper
- Subcommands: publish, fetch, status, topics
  - Publish JSON envelope:
    echo -n 'hello' | ./rclipctl publish -c --encoding base64 --json | jq .
  - Fetch JSON envelope:
    ./rclipctl fetch -c --json | jq .
  - Fetch (non‑JSON, convert encoding to hex):
    ./rclipctl fetch -c --encoding hex
  - Status / Topics:
    ./rclipctl status
    ./rclipctl topics

tmux Integration (Plugin)
- The repository includes a minimal tmux plugin in `tmux-rclipboard/`.
- With TPM (tmux plugin manager), add to your `~/.tmux.conf`:
  set -g @plugin 'tmux-plugins/tpm'
  set -g @plugin 'local/rclipboard-py/tmux-rclipboard'
  run '~/.tmux/plugins/tpm/tpm'

- Or source directly:
  run-shell '/path/to/repo/tmux-rclipboard/rclipboard.tmux'

- Options (with defaults):
  - `@rclip_host` (127.0.0.1), `@rclip_port` (8989)
  - `@rclip_topic` (c), `@rclip_encoding` (base64), `@rclip_app` (tmux)
  - `@rclip_status` (on) — add health segment to status-right
  - `@rclip_bin` (rclipctl)

- Key bindings installed by the plugin:
  - Copy in copy-mode-vi: `y` → publish to rclipboard
  - Paste: `P` → fetch from rclipboard into pane

- Status bar: adds `rc: up xsel:✓ pxy:✓` (colors) via `scripts/health.sh`.

X Clipboard (xsel) Sync
- Periodically polls X selections (clipboard -b, primary -p, secondary -s); user changes are published as DataItem (binary base64 with padding).
- Topic updates (c/p/s) are applied back to X using xsel.
- Check status:
  ./rclipctl status --host 127.0.0.1 --port 8989 | jq '.xsel'
  # or: curl -fsS http://127.0.0.1:8989/status | jq '.xsel'
- Configure:
  - RCLIPBOARD_XSEL: 1/0 (default 1)
  - RCLIPBOARD_XSEL_PATH: path to xsel (default /usr/bin/xsel)
  - RCLIPBOARD_XSEL_INTERVAL_MS: poll interval (default 500)

Proxy Mode (Remote Sync)
- Enable a background WS client which subscribes to upstream and forwards local publishes.
- Enable:
  - RCLIPBOARD_PROXY=1
  - RCLIPBOARD_PROXY_ADDR / RCLIPBOARD_PROXY_PORT (default 127.0.0.1:8989)
  - RCLIPBOARD_PROXY_UDS (optional; prefer TCP or SSH tunnel if unsupported)
- Health:
  ./rclipctl health --host 127.0.0.1 --port 8989 | jq .  # proxy_connected
  # or: curl -fsS http://127.0.0.1:8989/health | jq .

Transports
- TCP: uvicorn app.main:app --host 127.0.0.1 --port 8989
- Unix Domain Socket: uvicorn app.main:app --uds /tmp/rclip.sock
  - ./rclipctl status --uds /tmp/rclip.sock
  - curl --unix-socket /tmp/rclip.sock http://localhost/status
- HTTPS: uvicorn app.main:app --host 127.0.0.1 --port 8989 \
    --ssl-keyfile key.pem --ssl-certfile cert.pem

Self‑signed TLS (no external CA)
- Minimal (CN=localhost):
  ./scripts/gencert.sh --cn localhost
- With SAN (localhost, 127.0.0.1, ::1):
  ./scripts/gencert.sh --san
Note: some clients (e.g. curl) may require -k/--insecure for self‑signed certs.

Environment Variables
- RCLIPBOARD_BIND_ADDR / RCLIPBOARD_BIND_PORT — TCP bind (defaults 127.0.0.1:8989)
- RCLIPBOARD_BIND_UDS — UDS path (overrides host/port)
- RCLIPBOARD_SSL_CERTFILE / RCLIPBOARD_SSL_KEYFILE / RCLIPBOARD_SSL_KEYFILE_PASSWORD
- RCLIPBOARD_LOG_LEVEL — uvicorn log level (info/debug)
- RCLIPBOARD_XSEL, RCLIPBOARD_XSEL_PATH, RCLIPBOARD_XSEL_INTERVAL_MS
- RCLIPBOARD_PROXY, RCLIPBOARD_PROXY_ADDR, RCLIPBOARD_PROXY_PORT, RCLIPBOARD_PROXY_UDS
-
systemd (User Services)
- Install units and env:
  make systemd-user-install
  # edit ~/.config/rclipboard/env and adjust WorkingDirectory via override if needed
- Enable simple TCP service:
  systemctl --user enable --now rclipboard.service
- Or socket-activated UDS (XDG_RUNTIME_DIR/rclipboard.sock):
  systemctl --user enable --now rclipboard.socket
  ./rclipctl status --uds "$XDG_RUNTIME_DIR/rclipboard.sock"
  # or: curl --unix-socket "$XDG_RUNTIME_DIR/rclipboard.sock" http://localhost/status
- Proxy variant:
  systemctl --user enable --now rclipboard-proxy.service

Notes:
- Units read env from ~/.config/rclipboard/env.
- WorkingDirectory defaults to a repo path; the installer sets it to current repo.
- For X clipboard sync from a user session, ensure DISPLAY/XAUTHORITY are available in your environment.
