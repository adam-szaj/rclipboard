# rclipboard — User Manual

Tools covered: `rclipctl`, `rcliptunel`.

---

## rclipctl

Command-line client for rclipboard. Auto-detects transport: FIFO → UDS → TCP.

```
rclipctl <command> [options]
```

### Transport options (global)

These apply to every command that contacts the server.

| Option | Description |
|---|---|
| `--endpoint <url>` | `host:port`, `http://…`, `uds:///path`, `fifo:///dir` |
| `--transport <mode>` | `auto` \| `uds` \| `tcp` \| `fifo` (override auto-detect) |
| `--host <host>` | TCP host (default: `127.0.0.1`) |
| `--port <port>` | TCP port (default: `8989`) |
| `--uds <path>` | Unix domain socket path |
| `--config <file>` | Path to `config.toml` (default: `~/.config/rclipboard/config.toml`) |

---

### put / clip — write clipboard

Read stdin and send to the server.

```
rclipctl put   [topic] [options]
rclipctl clip  [topic] [options]    # alias for put
```

**Topic options**

| Option | Topic |
|---|---|
| `-t <name>` / `--topic <name>` | Named topic (default: `c`) |
| `-c` / `--clipboard` | Topic `c` (system clipboard) |
| `-p` / `--primary` | Topic `p` (X11 primary selection) |
| `-s` / `--secondary` | Topic `s` (X11 secondary selection) |

**Other options**

| Option | Description |
|---|---|
| `-a <app>` / `--app <app>` | Set `meta.app` tag (default: `rclipctl`) |
| `-e` / `--encoded` | Stdin is already base64-encoded |
| `--json` | Print raw JSON response |

**Encryption options**

| Option | Description |
|---|---|
| `-E` / `--encrypt` | Encrypt payload with age before sending |
| `--key <age1…>` | Recipient public key (repeatable) |
| `--key-file <path>` | File with recipient public keys (one per line) |
| `--fetch-keys` | Fetch recipient keys from `/v1/keys.list` |

**Examples**

```bash
# Basic put
echo "hello" | rclipctl put
date        | rclipctl put -t c --app myapp

# Put to primary selection
xsel -op    | rclipctl put -p

# Encrypted put (recipients from server registry)
echo "secret" | rclipctl put --encrypt --fetch-keys

# Encrypted put (explicit recipient)
echo "secret" | rclipctl put -E --key age1qqq...
```

---

### get — read clipboard

Fetch the current value of a topic.

```
rclipctl get [topic] [options]
```

**Options**

| Option | Description |
|---|---|
| `-t <name>` / `--topic <name>` | Topic (default: `c`) |
| `-c` / `-p` / `-s` | Shortcuts for topics `c`, `p`, `s` |
| `--encoding base64\|hex` | Binary output encoding |
| `--json` | Print raw JSON response |

When the item has `encrypted: true`, `rclipctl get` automatically decrypts it using `~/.config/rclipboard/age_key.txt`. No extra flag needed.

**Examples**

```bash
# Get clipboard topic
rclipctl get

# Get to X11
rclipctl get | xsel -ib

# Get with explicit topic
rclipctl get -t p

# Get raw JSON
rclipctl get --json
```

---

### exec — stdio encryption pipe

Run a command and encrypt/decrypt its stdio stream. No server communication — pure local pipe.

```
rclipctl exec --encrypt-output [enc-opts] -- <command> [args…]
rclipctl exec --decrypt-input             -- <command> [args…]
```

**Modes**

| Mode | Description |
|---|---|
| `--encrypt-output` | Run command, capture stdout, encrypt with age, print base64 to stdout |
| `--decrypt-input` | Decrypt base64 from stdin with local age key, feed plaintext to command's stdin |

**Encryption options** (for `--encrypt-output`)

| Option | Description |
|---|---|
| `--key <age1…>` | Recipient key (repeatable) |
| `--key-file <path>` | File with recipient keys |
| `--fetch-keys` | Fetch keys from server registry |

**Examples**

```bash
# Encrypt xsel output and store it
rclipctl exec --encrypt-output --fetch-keys -- xsel -ob | rclipctl put -e

# Decrypt clipboard and pipe to xsel
rclipctl get | rclipctl exec --decrypt-input -- xsel -ib

# Encrypt a file
cat secret.txt | rclipctl exec --encrypt-output --key age1qqq... -- cat
```

---

### keygen — generate age keypair

Generate an X25519 keypair for end-to-end encryption. Local operation, no server needed.

```
rclipctl keygen
```

Creates:
- `~/.config/rclipboard/age_key.txt` — private key (chmod 600)
- `~/.config/rclipboard/age_key.pub` — public key

If the key already exists, prints the public key and exits without overwriting.

---

### register — register public key with server

Send your public key to the server's in-memory registry.  
Requires an admin token (`RCLIPBOARD_ADMIN_TOKEN` on the server).

```
rclipctl register [options]
```

| Option | Description |
|---|---|
| `--token <token>` | Admin bearer token |
| `--label <label>` | Key label (default: hostname) |

```bash
rclipctl register --token my-secret-token
rclipctl register --token my-secret-token --label laptop
```

Returns `503` if the server has no admin token configured.  
Registry is in-memory: keys are lost on server restart.

---

### keys-list — list registered keys

```
rclipctl keys-list
```

Prints all public keys registered on the server.  
No auth required.

---

### health — server health check

```
rclipctl health
```

Returns JSON:

```json
{
  "ok": true,
  "xsel_enabled": false,
  "xsel_good": false,
  "proxy_enabled": false,
  "proxy_good": false
}
```

---

### status — server status

```
rclipctl status
```

Returns extended JSON including topic list, per-topic timestamps, connected WS clients, and proxy telemetry:

```json
{
  "ok": true,
  "topics": ["c", "p"],
  "topic_status": [
    { "topic": "c", "ts": "2026-04-21T10:00:00+00:00",
      "stub": false, "size": 5, "stored_ago": 1.2 }
  ],
  "proxy": {
    "enabled": true, "good": true,
    "endpoint": "127.0.0.1:8989",
    "connected": true,
    "rx_count": 42, "tx_count": 7,
    "last_rx_ago": 0.3
  }
}
```

---

### topics — list topics

```
rclipctl topics
```

Returns `{ "topics": ["c", "p", "vim"] }`.

---

## rcliptunel

SSH tunnel manager for rclipboard. Establishes a port-forward and optionally manages the proxy connection lifecycle.

```
rcliptunel --ssh <user@host> [options] [-- <extra ssh args>]
```

### Required

| Option | Description |
|---|---|
| `--ssh <user@host>` | SSH destination |

### Endpoint options

| Option | Description |
|---|---|
| `--local <endpoint>` | Local server endpoint (`tcp:HOST:PORT` or `uds:///path`) |
| `--remote <endpoint>` | Endpoint exposed on remote side (default: derived from `--local`) |
| `--ssh-port <port>` | SSH port (default: `22`) |

**Default local endpoint** (resolved in order):
1. `RCLIPBOARD_ENDPOINT` env var
2. `uds://$RCLIPBOARD_BIND_UDS` if set
3. `tcp:127.0.0.1:$RCLIPBOARD_BIND_PORT` (default port: `8989`)

**Default remote endpoint**: `tcp:127.0.0.1:8988`

### Tunnel direction

| Option | Description |
|---|---|
| `--reverse` | SSH `-R`: remote reaches local server (default) |
| `--forward` | SSH `-L`: local reaches remote server |

### Proxy automation

| Option | Description |
|---|---|
| `--proxy-auto` | Connect proxy on start, disconnect on exit (`trap EXIT`) |
| `--proxy-connect` | Send `proxy.connect` after tunnel is up, then exit |
| `--proxy-disconnect` | Send `proxy.disconnect`, then exit |
| `--token <token>` | Admin bearer token (or `RCLIPBOARD_ADMIN_TOKEN` env) |
| `--no-proxy-reconnect` | Set `reconnect=false` in `proxy.connect` |

`--proxy-auto` is the typical choice for interactive sessions: the proxy activates when you connect and deactivates automatically when you close the terminal.

### Other options

| Option | Description |
|---|---|
| `--login` / `-l` | Open interactive shell instead of port-forward-only (`-N`) |
| `--daemonize` / `-d` | Run tunnel in background |
| `--config <file>` | Path to `config.toml` |
| `-h` / `--help` | Show help |

### Examples

**Reverse tunnel** (remote server reachable locally via forwarded port):

```bash
rcliptunel --ssh user@remote --reverse \
           --local tcp:127.0.0.1:8989 \
           --remote tcp:127.0.0.1:8988
```

Remote's port 8988 is forwarded to local port 8989.

**Forward tunnel** (local server reachable from remote):

```bash
rcliptunel --ssh user@remote --forward \
           --local tcp:127.0.0.1:8989 \
           --remote tcp:127.0.0.1:8988
```

**Fully automatic proxy session**:

```bash
rcliptunel --ssh user@remote --proxy-auto --token my-secret-token
# Tunnel opens → proxy.connect sent → clipboard syncs
# Ctrl+C → proxy.disconnect sent → tunnel closes
```

**Daemonised tunnel with manual proxy connect**:

```bash
rcliptunel --ssh user@remote --daemonize
rcliptunel --ssh user@remote --proxy-connect --token my-secret-token
# ... work ...
rcliptunel --ssh user@remote --proxy-disconnect --token my-secret-token
```

**Login shell** (interactive session + tunnel in background):

```bash
rcliptunel --ssh user@remote --proxy-auto --login --token my-secret-token
```

**Extra SSH arguments** (pass after `--`):

```bash
rcliptunel --ssh user@remote --proxy-auto --token tok -- -i ~/.ssh/id_rsa -o ServerAliveInterval=30
```

---

## Tips

**Check what transport rclipctl is using**

```bash
rclipctl health --json   # will fail fast if transport is wrong
```

**Pipe clipboard between machines**

```bash
# On machine A (push):
echo "data" | rclipctl put

# On machine B (connected via proxy or tunnel):
rclipctl get
```

**Watch clipboard changes** (WebSocket, requires `websocat` or similar):

```bash
websocat ws://127.0.0.1:8989/ws
# send: {"jsonrpc":"2.0","id":1,"method":"clip.watch","params":{"topics":["c"]}}
```

**Inspect config resolution**:

```bash
rclipboard config env    # prints all resolved env vars
```
