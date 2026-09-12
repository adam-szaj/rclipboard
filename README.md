# RemoteClipboard (rclipboard)

`rclipboard` is a lightweight clipboard/message-bus server for local and remote
workflows. It provides:

- an HTTP API
- a WebSocket API based on JSON-RPC 2.0
- a local proxy that synchronises with an upstream server over WebSocket
- native text clipboard integration for macOS via `pbcopy` and `pbpaste`
- optional client-side end-to-end encryption using the [age](https://age-encryption.org/) format
- simple CLI tools for `put/get/exec/keygen/register/status/topics/health`

The current API contract is described in [docs/api-contract.md](docs/api-contract.md).

## Core Principles

- one shared data model for HTTP, WS, and proxy
- HTTP maps an endpoint to a `method`, and the body carries `params/result/error`
- WS uses full JSON-RPC 2.0 envelopes
- the proxy keeps a single upstream connection and replicates changes locally
- the server is a **blind store** — encryption/decryption is always client-side

## Implemented endpoints

- `POST /v1/clip.put`
- `POST /v1/clip.get` (gated on `X-Age-Public-Key` when content is encrypted)
- `POST /v1/topics.list`
- `GET /v1/health.get`
- `GET /v1/status.get`
- `POST /v1/keys.publish` (admin token required)
- `GET /v1/keys.list`
- `WS /ws` with methods:
  - `clip.put`, `clip.get`, `clip.watch`, `clip.unwatch`
  - `topics.list`, `health.get`, `status.get`
- server event: `clip.changed`
- proxy: upstream `clip.watch`, forwarding local `clip.put`, receipt of upstream `clip.changed`

## Installation

The user-service installer supports Linux (`systemd --user`) and macOS
(`launchd`). It requires Python 3.11+, Git, Bash, and a Git checkout with an
attached branch. From the repository root:

```bash
./scripts/install.sh
```

Installation is private to the current user and defaults to a Unix Domain
Socket. Use `./scripts/install.sh --transport tcp` only when an SSH setup cannot
forward UDS; the TCP listener binds to `127.0.0.1:8989`. The installed
layout is `${XDG_CONFIG_HOME:-$HOME/.config}/rclipboard`; add its `bin/`
directory to `PATH` to use the installed commands.

```bash
# Service status
systemctl --user status rclipboard.service                         # Linux
launchctl print gui/$(id -u)/com.rclipboard.service                # macOS

# Lifecycle
rclipboard-update [--remote REMOTE] [--branch BRANCH]
./scripts/install.sh --reset [--purge-user-data]
rclipboard-uninstall [--purge-user-data]
```

Update is clean-worktree and fast-forward-only. Reset and uninstall preserve
`config.toml` and key files unless purge is requested and the user types the
literal lowercase `yes` through an interactive terminal.

See [docs/INSTALL.md](docs/INSTALL.md) for requirements, installed paths and
`PATH`, Linux/macOS status and logs, safe update rules, reset/uninstall behavior,
and the exact confirmation required before configuration or keys can be
deleted.

On a new macOS installation, the same per-user LaunchAgent synchronizes the
native text clipboard with topic `c`; no additional package is required.

### Native macOS clipboard

The macOS adapter polls the system pasteboard with `/usr/bin/pbpaste` and
writes remote topic `c` updates with `/usr/bin/pbcopy`. It handles
text-compatible content only; images, rich text, file references, and X11
primary/secondary selections are not synchronized. A 250 ms poll interval and
the last seen/applied value prevent a remote update from being published back
as a new local change.

New macOS installations enable the adapter automatically. Reinstall and update
do not rewrite an existing `config.toml`; older installations can opt in with:

```toml
[pasteboard]
enabled      = true
pbcopy_path  = "/usr/bin/pbcopy"
pbpaste_path = "/usr/bin/pbpaste"
interval_ms  = 250
```

Restart the LaunchAgent after changing the configuration:

```bash
launchctl kickstart -k gui/$(id -u)/com.rclipboard.service
rclipctl health  # pasteboard_enabled=true, pasteboard_good=true
```

For a manual round-trip check:

```bash
printf 'from macOS' | pbcopy
sleep 1
rclipctl get -c

printf 'from rclipboard' | rclipctl put -c
sleep 1
pbpaste
```

## Running

The user service installed above uses the private UDS configuration by default.
For manual development runs, the server can instead be started on loopback TCP:

```bash
RCLIPBOARD_ENDPOINT=127.0.0.1:8989 .venv/bin/rclipboard
```

TCP:

```bash
PYTHONPATH=src .venv/bin/python -m uvicorn rclipboard.main:app \
  --host 127.0.0.1 --port 8989
```

UDS:

```bash
PYTHONPATH=src .venv/bin/python -m uvicorn rclipboard.main:app \
  --uds /tmp/rclip.sock
```

HTTPS:

```bash
./scripts/gencert.sh --san
PYTHONPATH=src .venv/bin/python -m uvicorn rclipboard.main:app \
  --host 127.0.0.1 --port 8989 \
  --ssl-keyfile key.pem --ssl-certfile cert.pem
```

## Quick CLI Start

`rclipctl` reads the installed `config.toml` and the private generated runtime
environment. A new service installation uses:

```toml
[server]
endpoint = "uds://${XDG_RUNTIME_DIR}/rclipboard/uds.sock"

[client]
transport = "uds"
```

Accepted formats:

- `127.0.0.1:8989`
- `http://127.0.0.1:8989`
- `https://host:port`
- `uds:///run/user/1000/rclipboard.sock`

Client-side transport selection can be overridden explicitly:

- env override: `RCLIPCTL_TRANSPORT=auto|uds|tcp`
- CLI override: `--transport auto|uds|tcp`

Health:

```bash
rclipctl health
```

Put:

```bash
echo -n 'hello' | rclipctl put -c
```

Get in JSON format:

```bash
rclipctl get -c --json | jq .
```

Get as hex:

```bash
rclipctl get -c --encoding hex
```

List topics:

```bash
rclipctl topics | jq .
```

Encrypt and send:

```bash
echo -n 'secret' | rclipctl put --encrypt -c
```

Receive and decrypt (opt-in):

```bash
rclipctl get -c --decrypt
```

Use `exec` as a stdio encryption pipe (no server communication):

```bash
# Encrypt stdout of a command, print ciphertext to stdout
rclipctl exec --encrypt-output --key age1... -- xsel -ob

# Decrypt stdin and pipe plaintext to a command
rclipctl exec --decrypt-input -- xsel -ib
```

## End-to-End Encryption

rclipboard supports optional client-side encryption using the [age](https://age-encryption.org/)
format. The server never sees plaintext — it stores and forwards ciphertext unchanged.

### Setup

```bash
# 1. Install age (https://github.com/FiloSottile/age#installation)

# 2. Generate a keypair
rclipctl keygen
# → ~/.config/rclipboard/age_key.txt  (private, chmod 600)
# → ~/.config/rclipboard/age_key.pub  (public)

# 3. Register your public key with the server
rclipctl register --token <admin_token>

# 4. Encrypt and send
echo "secret" | rclipctl put --encrypt --fetch-keys

# 5. Receive and decrypt on any registered machine
rclipctl get --decrypt
```

### Key registry

```bash
# List registered public keys (open, no auth)
curl http://127.0.0.1:8989/v1/keys.list | jq .

# Register a public key (requires admin token)
curl -X POST http://127.0.0.1:8989/v1/keys.publish \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <admin_token>' \
  -d '{"public_key":"age1...","label":"laptop"}'
```

The admin token is set via `RCLIPBOARD_ADMIN_TOKEN` env var or `config.toml [server] admin_token`.
The registry is in-memory only (lost on server restart).

### Access control

- Unencrypted topics: freely accessible to all clients.
- Encrypted topics: `clip.get` requires an `X-Age-Public-Key: age1...` header whose key is
  registered in the server. Returns `403` otherwise. `rclipctl get` sends this header
  automatically if `~/.config/rclipboard/age_key.pub` exists.

### X11 clipboard encryption (xsel)

When `RCLIPBOARD_XSEL_ENCRYPT=1` (or `[xsel] encrypt = true` in config.toml), xsel
routes clipboard reads and writes through `rclipctl exec` instead of calling `xsel` directly:

```toml
[xsel]
enabled = true
encrypt = true   # requires age + rclipctl on PATH and registered public keys
```

### `rclipctl exec` — stdio encryption pipe

```bash
# Encrypt stdout of any command, print ciphertext to stdout
rclipctl exec --encrypt-output [--key age1...] [--fetch-keys] -- <cmd>

# Decrypt stdin and pipe plaintext to a command's stdin
rclipctl exec --decrypt-input -- <cmd>
```

`exec` has no server communication — it is a composable stdio filter.

## HTTP API

### `POST /v1/clip.put`

Request body:

```json
{
  "items": [
    {
      "topic": "c",
      "mime": "text/plain",
      "encoding": "utf-8",
      "value": "hello"
    }
  ],
  "meta": {
    "app": "curl"
  }
}
```

Example:

```bash
curl -fsS -X POST http://127.0.0.1:8989/v1/clip.put \
  -H 'Content-Type: application/json' \
  -d '{"items":[{"topic":"c","mime":"text/plain","encoding":"utf-8","value":"hello"}],"meta":{"app":"curl"}}' | jq .
```

### `POST /v1/clip.get`

Request body:

```json
{
  "topic": "c"
}
```

Example:

```bash
curl -fsS -X POST http://127.0.0.1:8989/v1/clip.get \
  -H 'Content-Type: application/json' \
  -d '{"topic":"c"}' | jq .
```

Not found:

```json
{
  "code": 1001,
  "message": "Topic not found",
  "data": {
    "topic": "c"
  }
}
```

### `POST /v1/topics.list`

```bash
curl -fsS -X POST http://127.0.0.1:8989/v1/topics.list \
  -H 'Content-Type: application/json' \
  -d '{}' | jq .
```

### `GET /v1/health.get`

```bash
curl -fsS http://127.0.0.1:8989/v1/health.get | jq .
```

### `GET /v1/status.get`

```bash
curl -fsS http://127.0.0.1:8989/v1/status.get | jq .
```

## WebSocket API

Endpoint:

```text
ws://127.0.0.1:8989/ws
```

### `clip.watch`

Request:

```json
{
  "jsonrpc": "2.0",
  "id": "watch-1",
  "method": "clip.watch",
  "params": {
    "topics": ["c"]
  }
}
```

Response:

```json
{
  "jsonrpc": "2.0",
  "id": "watch-1",
  "result": {
    "topics": ["c"]
  }
}
```

### `clip.put`

```json
{
  "jsonrpc": "2.0",
  "id": "put-1",
  "method": "clip.put",
  "params": {
    "items": [
      {
        "topic": "c",
        "mime": "text/plain",
        "encoding": "utf-8",
        "value": "hello"
      }
    ],
    "meta": {
      "app": "ws-client"
    }
  }
}
```

### `clip.get`

```json
{
  "jsonrpc": "2.0",
  "id": "get-1",
  "method": "clip.get",
  "params": {
    "topic": "c"
  }
}
```

### Event `clip.changed`

After subscribing, the client receives a notification:

```json
{
  "jsonrpc": "2.0",
  "method": "clip.changed",
  "params": {
    "items": [
      {
        "topic": "c",
        "mime": "text/plain",
        "encoding": "utf-8",
        "value": "hello"
      }
    ],
    "meta": {
      "app": "ws-client"
    }
  }
}
```

## Proxy

The proxy is a local `rclipboard` server that also keeps a WS connection to an
upstream server.

How it works:

1. the proxy runs a local HTTP/WS server
2. the proxy connects upstream over WebSocket
3. once connected, it sends `clip.watch` for topics `c`, `p`, and `s`
4. when a local client sends `clip.put` to the proxy, the proxy forwards it upstream
5. when the upstream sends `clip.changed`, the proxy stores the change locally
6. remote programs talk to the local proxy instead of talking to the upstream directly

This reduces cross-host round trips when working over SSH.

### Running the Proxy

```bash
RCLIPBOARD_ENDPOINT=127.0.0.1:8789 \
RCLIPBOARD_PROXY=1 \
RCLIPBOARD_UPSTREAM_ENDPOINT=127.0.0.1:8989 \
.venv/bin/rclipboard
```

### Proxy Smoke Test

```bash
make proxy-smoke
```

This target:

- starts an upstream server
- starts a local proxy
- verifies `upstream -> proxy`
- verifies `proxy -> upstream`

## SSH Tunnel (`rcliptunel`)

`rcliptunel` is a wrapper around `ssh -R` / `ssh -L` that creates a tunnel
between a local rclipboard endpoint and a remote host.  All four socket-type
combinations are supported:

| Local | Remote | Direction |
|-------|--------|-----------|
| TCP   | TCP    | `ssh -R remote_host:remote_port:local_host:local_port` |
| UDS   | TCP    | `ssh -R remote_host:remote_port:local_socket` |
| TCP   | UDS    | `ssh -R remote_socket:local_host:local_port` |
| UDS   | UDS    | `ssh -R remote_socket:local_socket` |

UDS forwarding requires **OpenSSH ≥ 6.7** on both the client and the server.

### Required sshd configuration on the remote host

Add (or verify) the following lines in `/etc/ssh/sshd_config` on the remote server:

```
# Allow TCP port forwarding (needed for TCP remote endpoints)
AllowTcpForwarding yes

# Allow Unix Domain Socket forwarding (needed for UDS remote endpoints)
# Requires OpenSSH ≥ 6.7
AllowStreamLocalForwarding yes

# Auto-remove stale UDS socket files left by previous tunnel sessions
StreamLocalBindUnlink yes

# Let the client choose the bind address for remote forwards.
# Without this, OpenSSH forces the remote listen address to 127.0.0.1,
# which is usually fine, but explicit "host:port" specs in -R require it.
GatewayPorts clientspecified
```

Reload sshd after editing:

```bash
# systemd
sudo systemctl reload sshd

# OpenRC
sudo rc-service sshd reload
```

### Usage examples

Share a local UDS server with the remote machine (remote proxy connects via TCP):

```bash
rcliptunel \
  --local  uds:///run/user/1000/rclipboard/uds.sock \
  --remote tcp:127.0.0.1:8988 \
  --ssh    user@remotehost
```

Share a local TCP server with the remote machine via a remote UDS socket:

```bash
rcliptunel \
  --local  tcp:127.0.0.1:8989 \
  --remote uds:///run/user/1000/rclipboard/proxy.sock \
  --ssh    user@remotehost
```

Forward a remote TCP server to a local UDS socket (`--forward` = `ssh -L`):

```bash
rcliptunel \
  --local  uds:///run/user/1000/rclipboard/proxy.sock \
  --remote tcp:127.0.0.1:8989 \
  --ssh    user@remotehost \
  --forward
```

Run in the background and read settings from `config.toml`:

```bash
rcliptunel \
  --ssh user@remotehost \
  --config ~/.config/rclipboard/config.toml \
  --daemonize
```

### TOML configuration

`rcliptunel` reads the same `config.toml` as the server when `--config` is
given, or sources the private generated environment below
`${XDG_RUNTIME_DIR:-$HOME/.local/run}/rclipboard/env` when it exists. The
relevant fields are `server.endpoint` (used as the default `--local` value) and
the proxy `upstream_endpoint` port (used as the default `--remote` TCP port).

## Makefile

Most important targets:

- `make run`
- `make run-uds`
- `make run-proxy`
- `make smoke`
- `make proxy-smoke`
- `make test`
- `make test-functional`
- `make test-integration`
- `make test-http`
- `make test-ws`
- `make test-proxy-integration`
- `make test-platform-clipboard` — xsel on Linux, native pasteboard on macOS
- `make docker-build-tunel-test` — build the SSH test image (required once before `test-tunel`)
- `make test-tunel` — SSH tunnel smoke tests (requires Docker)

## Tests

Automated tests live in `tests/`:

- `test_functional_http.py` — HTTP REST endpoints
- `test_functional_ws.py` — WebSocket JSON-RPC
- `test_functional_keys.py` — key registry + encrypted clip.get access control
- `test_functional_https.py` / `test_functional_wss.py` — TLS variants
- `test_integration_proxy.py` / `test_integration_proxy_ssl.py` — proxy integration
- `test_integration_tunel.py` — SSH tunnel smoke tests (require Docker)
- `test_xsel_display_env.py` — Linux/X11 clipboard integration
- `test_pasteboard.py` — portable macOS adapter and lifecycle tests
- `test_pasteboard_macos.py` — native `/usr/bin/pbcopy` and
  `/usr/bin/pbpaste` round trips on macOS

Run all tests:

```bash
make test
```

Or run them individually:

```bash
make test-http
make test-ws
make test-proxy-integration
make test-platform-clipboard  # xsel on Linux, native pbcopy/pbpaste on macOS

# SSH tunnel tests — build the test image first:
make docker-build-tunel-test
make test-tunel
```

The tunnel tests start a Docker container with `openssh-server` and verify all four
socket-type combinations (`tcp→tcp`, `uds→tcp`, `tcp→uds`, `uds→uds`).  They skip
gracefully when Docker is not available or the test image has not been built.

The [platform clipboard workflow](.github/workflows/platform-clipboard.yml)
runs the same `make test-platform-clipboard` target on Linux and macOS. Linux
runs the `xsel` suite. macOS replaces it with the portable pasteboard suite and
native tests that use the real user pasteboard, verify Unicode in both
directions, suppress write-back echo, and restore the previous text content.

## Environment Configuration

**Server:**
- `RCLIPBOARD_ENDPOINT` — bind address (`host:port`, UDS path, `https://`)
- `RCLIPBOARD_LOG_LEVEL` / `RCLIPBOARD_PY_LOG_LEVEL`
- `RCLIPBOARD_NOTIFY_DELAY_MS` — debounce delay for `clip.changed` (default: 250)
- `RCLIPBOARD_RELOAD` — enable uvicorn reload mode
- `RCLIPBOARD_SSL_CERTFILE` / `RCLIPBOARD_SSL_KEYFILE` / `RCLIPBOARD_SSL_KEYFILE_PASSWORD`
- `RCLIPBOARD_PROXY` / `RCLIPBOARD_UPSTREAM_ENDPOINT`
- `RCLIPBOARD_XSEL` / `RCLIPBOARD_XSEL_PATH` / `RCLIPBOARD_XSEL_INTERVAL_MS`
- `RCLIPBOARD_XSEL_ENCRYPT` — encrypt X11 clipboard via `rclipctl exec` (default: `0`)
- `RCLIPBOARD_PASTEBOARD` — enable macOS text clipboard integration
- `RCLIPBOARD_PBCOPY_PATH` / `RCLIPBOARD_PBPASTE_PATH` — native tool paths
- `RCLIPBOARD_PASTEBOARD_INTERVAL_MS` — pasteboard poll interval (default: 250)
- `RCLIPCTL_PATH` — path to `rclipctl` used by xsel encrypt mode
- `RCLIPBOARD_ADMIN_TOKEN` — bearer token for `POST /v1/keys.publish`

**Client (`rclipctl`):**
- `RCLIPCTL_TRANSPORT` — `auto|uds|tcp`
- `RCLIPCTL_ENDPOINT`
- `RCLIPBOARD_AGE_KEY_FILE` — age private key (default: `~/.config/rclipboard/age_key.txt`)
- `RCLIPBOARD_KNOWN_KEYS_FILE` — recipient public keys file
- `RCLIPBOARD_ADMIN_TOKEN` — default token for `rclipctl register`

The old `RCLIPBOARD_BIND_*` and `RCLIPBOARD_UPSTREAM_{ADDR,PORT,UDS}` variables are
still read as backwards-compatibility fallbacks, but the current configuration
contract is based on `RCLIPBOARD_ENDPOINT` and `RCLIPBOARD_UPSTREAM_ENDPOINT`.

`RCLIPBOARD_NOTIFY_DELAY_MS` configures debouncing for `clip.changed` and other
fan-out triggered by `clip.put`. Topic state is updated immediately, but the
broadcast may be delayed and coalesced to the latest value. `clip.get` flushes
any pending notification for the requested topic before returning.

The installed configuration selects HTTP over UDS without automatic TCP
fallback. TCP can be selected explicitly via configuration or `--transport` on
the command line. See [docs/INSTALL.md](docs/INSTALL.md) for the private service
transport policy.

## Notes

- `README` documents the current implementation contract, not an older compatibility protocol.
- `docs/api-contract.md` is the source of truth for `params/result/error` models.
- `xsel` integration is still less mature than HTTP, WS, and proxy.
