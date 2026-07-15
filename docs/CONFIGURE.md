# rclipboard — Configuration Guide

All configuration lives in `~/.config/rclipboard/config.toml`.  
The server reads it at startup via `ExecStartPre` (systemd) or directly when invoked with `rclipboard config env`.  
Every TOML field has an equivalent environment variable — env always wins.

---

## Scenarios

### 1. Local single-machine (minimal)

The simplest setup: one server, TCP on localhost, no proxy, no encryption.

**Install**

```bash
make systemd-user-install      # or: ./scripts/install-systemd-user.sh
```

This creates:
- `~/.config/rclipboard/venv/` — Python venv with the `rclipboard` package
- `~/.config/rclipboard/config.toml` — starter config (from `scripts/systemd/user/rclipboard.conf.example`)
- `~/.config/systemd/user/rclipboard.service`
- `~/.local/bin/rclipctl`, `rcliptunel` — CLI tools

**config.toml**

```toml
[server]
endpoint = "127.0.0.1:8989"
log_level = "warning"
```

**Start**

```bash
systemctl --user enable --now rclipboard.service
systemctl --user status rclipboard.service
```

**Verify**

```bash
rclipctl health
echo "hello" | rclipctl put
rclipctl get
```

---

### 2. Unix Domain Socket (low-latency local)

Replaces TCP with a UDS for lower overhead when all clients are local.

```toml
[server]
endpoint = "uds://${XDG_RUNTIME_DIR}/rclipboard/uds.sock"

[client]
transport = "uds"
```

`rclipctl` picks up the UDS endpoint automatically via config.

---

### 3. X11 clipboard integration (xsel)

Keeps the server's topic `c` in sync with the X11 clipboard.

**Requirement**: `xsel` installed (`apt install xsel` / `pacman -S xsel`).

```toml
[xsel]
enabled      = true
path         = "/usr/bin/xsel"
interval_ms  = 250          # poll X11 every 250 ms
```

With X11 encryption (see Scenario 7):

```toml
[xsel]
enabled  = true
encrypt  = true             # capture via rclipctl exec --encrypt-output
```

---

### 4. HTTPS / TLS

**Generate a self-signed certificate**

```bash
# Basic (localhost CN)
./scripts/gencert.sh --key server.key --cert server.crt

# With SANs for 127.0.0.1 and localhost
./scripts/gencert.sh --san --key server.key --cert server.crt

# Custom validity
./scripts/gencert.sh --san --days 730 --key server.key --cert server.crt --cn myhost
```

Or with `make`:

```bash
make cert        # CN=localhost, no SANs
make cert-san    # CN=localhost + SANs
```

**config.toml**

```toml
[server]
endpoint = "https://127.0.0.1:8989"

[ssl]
certfile = "/path/to/server.crt"
keyfile  = "/path/to/server.key"
# keyfile_password = "..."   # if key is encrypted
```

**rclipctl with HTTPS**

```bash
rclipctl --endpoint https://127.0.0.1:8989 health
```

Or in config:

```toml
[client]
endpoint = "https://127.0.0.1:8989"
```

---

### 5. Proxy mode (two-machine clipboard sharing)

One machine runs upstream, the other runs a proxy that replicates all topics.

**Upstream machine** — standard server config:

```toml
[server]
endpoint = "127.0.0.1:8989"
```

**Proxy machine** — connects to upstream:

```toml
[server]
endpoint = "127.0.0.1:8989"

[proxy]
enabled           = true
upstream_endpoint = "upstream-host:8989"   # or wss:// for TLS
```

Proxy mode is a single service — no separate systemd unit needed.  
Enable/disable via `config.toml` or dynamically at runtime:

```bash
# Runtime connect (requires admin token)
rclipctl proxy-connect --endpoint upstream-host:8989 --token <ADMIN_TOKEN>

# Runtime disconnect
rclipctl proxy-disconnect --token <ADMIN_TOKEN>
```

**Proxy with SSH tunnel** — see Scenario 8.

**Lazy sync** (large payloads):

```toml
[proxy]
enabled              = true
upstream_endpoint    = "upstream-host:8989"
lazy_upstream_kb     = 256    # skip upstream put for payloads ≥ 256 KB
lazy_local_kb        = 64     # send stub to WS clients for payloads ≥ 64 KB
upstream_put_mode    = "debounced"   # immediate | debounced | on_demand
upstream_sync_delay_ms = 3000
```

---

### 6. End-to-end encryption (age)

The server is a **blind store** — it never sees plaintext. Encryption is entirely client-side using [age](https://github.com/FiloSottile/age).

**Requirements**: `age` and `age-keygen` installed.

**Setup (each participant)**

```bash
# 1. Generate keypair
rclipctl keygen
# → ~/.config/rclipboard/age_key.txt  (private, chmod 600)
# → ~/.config/rclipboard/age_key.pub  (public)

# 2. Configure admin token on server
#    (add to config.toml or env on the server machine)
```

**config.toml (server side)**

```toml
[server]
admin_token = "my-secret-token"   # required for key registration
```

**Register public key**

```bash
rclipctl register --token my-secret-token
# or with custom label:
rclipctl register --token my-secret-token --label laptop
```

**Send encrypted clipboard**

```bash
echo "secret text" | rclipctl put --encrypt --fetch-keys
# --fetch-keys fetches all registered public keys from /v1/keys.list
# --key age1...  to specify recipients manually
# --key-file ~/.config/rclipboard/known_keys  for a local key list
```

**Receive (decrypt explicitly)**

```bash
rclipctl get --decrypt
```

**X11 xsel with encryption**

```toml
[xsel]
enabled  = true
encrypt  = true
```

Requires at least one registered public key on the server. `rclipctl` must be on PATH.

**Keys**

```toml
[encryption]
key_file        = "~/.config/rclipboard/age_key.txt"
known_keys_file = "~/.config/rclipboard/known_keys"   # one age1... per line
```

**List registered keys**

```bash
rclipctl keys-list
```

---

### 7. SSH tunnel with proxy automation (rcliptunel)

Connects two machines through an SSH port-forward, then wires up the proxy automatically.

**Forward tunnel** (local server reachable from remote):

```bash
rcliptunel --ssh user@remote --forward \
           --local tcp:127.0.0.1:8989 \
           --remote tcp:127.0.0.1:8988
```

**Reverse tunnel** (remote server reachable locally):

```bash
rcliptunel --ssh user@remote --reverse \
           --local tcp:127.0.0.1:8989 \
           --remote tcp:127.0.0.1:8988
```

**Fully automatic proxy lifecycle** (`--proxy-auto`):

```bash
rcliptunel --ssh user@remote \
           --proxy-auto \
           --token my-secret-token
# On start: opens tunnel → sends proxy.connect pointing at tunnel port
# On Ctrl+C / exit: sends proxy.disconnect automatically (trap EXIT)
```

**Admin token** for proxy operations:

```toml
[server]
admin_token = "my-secret-token"
```

Or via env: `RCLIPBOARD_ADMIN_TOKEN=my-secret-token`.

---

### 8. Docker / containerised

**Build**

```bash
make docker-build              # builds rclipboard:latest
```

**Run (standalone)**

```bash
docker run --rm -p 8989:8989 \
  -e RCLIPBOARD_BIND_ADDR=0.0.0.0 \
  -e RCLIPBOARD_BIND_PORT=8989 \
  rclipboard:latest
```

**Run (proxy mode)**

```bash
docker run --rm -p 8989:8989 \
  -e RCLIPBOARD_BIND_ADDR=0.0.0.0 \
  -e RCLIPBOARD_BIND_PORT=8989 \
  -e RCLIPBOARD_PROXY=1 \
  -e RCLIPBOARD_UPSTREAM_ENDPOINT=upstream-host:8989 \
  rclipboard:latest
```

**Multi-container** (docker-compose.yml in repo root):

```bash
docker compose up
# upstream on :8989, proxy on :8988, client container with rclipctl/rcliptunel
```

---

## Full config.toml reference

```toml
[server]
endpoint            = "127.0.0.1:8989"   # host:port | uds://path | https://...
raw_uds_path        = ""                  # JSON-RPC 2.0 NDJSON socket (empty = disabled)
log_level           = "warning"           # debug | info | warning | error
py_log_level        = "WARNING"
notify_delay_ms     = 250                 # clip.changed debounce (ms)
admin_token         = ""                  # Bearer token for /v1/keys.publish, proxy.connect
reload              = false               # uvicorn --reload (dev only)

[xsel]
enabled             = false
path                = "/usr/bin/xsel"
interval_ms         = 250
encrypt             = false

[proxy]
enabled             = false
upstream_endpoint   = ""                  # host:port | wss://... | uds:///path
lazy_upstream_kb    = 0                   # 0 = disabled
lazy_local_kb       = 0                   # 0 = disabled
upstream_put_mode   = "immediate"         # immediate | debounced | on_demand
upstream_sync_delay_ms = 5000

[ssl]
certfile            = ""
keyfile             = ""
keyfile_password    = ""

[client]
transport           = ""                  # auto | tcp | uds (for rclipctl)
endpoint            = ""                  # override rclipctl target endpoint

[encryption]
key_file            = "~/.config/rclipboard/age_key.txt"
known_keys_file     = "~/.config/rclipboard/known_keys"
```

Variable expansion (`${VAR}` or `$VAR`) is supported in all string values.

---

## Environment variable reference

All env vars override their config.toml equivalents.

| Variable | config.toml equivalent | Default |
|---|---|---|
| `RCLIPBOARD_ENDPOINT` | `server.endpoint` | `127.0.0.1:8989` |
| `RCLIPBOARD_RAW_UDS_PATH` | `server.raw_uds_path` | — |
| `RCLIPBOARD_LOG_LEVEL` | `server.log_level` | `warning` |
| `RCLIPBOARD_PY_LOG_LEVEL` | `server.py_log_level` | `WARNING` |
| `RCLIPBOARD_NOTIFY_DELAY_MS` | `server.notify_delay_ms` | `250` |
| `RCLIPBOARD_ADMIN_TOKEN` | `server.admin_token` | — |
| `RCLIPBOARD_RELOAD` | `server.reload` | `0` |
| `RCLIPBOARD_XSEL` | `xsel.enabled` | `0` |
| `RCLIPBOARD_XSEL_PATH` | `xsel.path` | `/usr/bin/xsel` |
| `RCLIPBOARD_XSEL_INTERVAL_MS` | `xsel.interval_ms` | `250` |
| `RCLIPBOARD_XSEL_ENCRYPT` | `xsel.encrypt` | `0` |
| `RCLIPBOARD_PROXY` | `proxy.enabled` | `0` |
| `RCLIPBOARD_UPSTREAM_ENDPOINT` | `proxy.upstream_endpoint` | — |
| `RCLIPBOARD_LAZY_UPSTREAM_KB` | `proxy.lazy_upstream_kb` | `0` |
| `RCLIPBOARD_LAZY_LOCAL_KB` | `proxy.lazy_local_kb` | `0` |
| `RCLIPBOARD_UPSTREAM_PUT_MODE` | `proxy.upstream_put_mode` | `immediate` |
| `RCLIPBOARD_UPSTREAM_SYNC_DELAY_MS` | `proxy.upstream_sync_delay_ms` | `5000` |
| `RCLIPBOARD_SSL_CERTFILE` | `ssl.certfile` | — |
| `RCLIPBOARD_SSL_KEYFILE` | `ssl.keyfile` | — |
| `RCLIPBOARD_SSL_KEYFILE_PASSWORD` | `ssl.keyfile_password` | — |
| `RCLIPBOARD_AGE_KEY_FILE` | `encryption.key_file` | `~/.config/rclipboard/age_key.txt` |
| `RCLIPBOARD_KNOWN_KEYS_FILE` | `encryption.known_keys_file` | `~/.config/rclipboard/known_keys` |
| `RCLIPCTL_ENDPOINT` | `client.endpoint` | — |
| `RCLIPBOARD_ADMIN_TOKEN` | `server.admin_token` | — |
