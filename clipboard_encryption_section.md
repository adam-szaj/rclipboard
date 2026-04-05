# Encryption

## Concept

End-to-end encryption using the `age` format (RFC-compatible, implemented via `pyrage`).
The service is a **blind store** — it never sees plaintext. Encryption and decryption
happen exclusively on the client side.

Sensitive entries are encrypted **before** reaching the API. Non-sensitive entries
(regular copy-paste) are stored as plaintext. The entry schema has an `encrypted` flag
that tells the receiving client whether to decrypt.

## Clipboard entry schema

```json
{
  "id": "uuid",
  "timestamp": "ISO8601",
  "encrypted": true,
  "payload": "<age-armored-ciphertext>"
}
```

When `encrypted: false`, `payload` is a plain string.

## Key model

- Each client generates an **X25519 keypair** (via `age-keygen` or `pyrage`).
- The **private key** never leaves the client. Storage location is the client's
  responsibility (e.g. `~/.config/age/key.txt`, keyring, etc.).
- The **public key** is submitted to the service during registration and stored
  server-side. It acts as the encryption target for other clients.
- Only registered clients may push entries (authentication required).
  The service does not verify who encrypted what — it only verifies that
  the sender is a registered client.

## Registration flow

```
POST /clients/register
{
  "public_key": "age1..."
}

Response:
{
  "client_id": "<uuid or token — TBD>",
  "auth_token": "<bearer token>"
}
```

> **Note:** The client identity scheme (UUID, username, etc.) is not yet decided.
> The `client_id` field should be treated as opaque for now. All authenticated
> endpoints use `Authorization: Bearer <auth_token>`.

## Encryption flow (client side)

Before pushing a sensitive entry:

1. Fetch the current list of registered public keys from the service.
2. Encrypt the plaintext once for **all recipients** using `age` multi-recipient mode.
   This produces a single ciphertext blob — one blob, N recipients.
3. Set `encrypted: true` in the entry payload.
4. Push the entry.

Cache the recipient list and refresh it only when the client list changes,
not on every push.

### Python (pyrage)

```python
from pyrage import encrypt, x25519

# Load once at startup, not per-entry
recipients = [x25519.Recipient.from_str(k) for k in public_keys]

def push_entry(plaintext: str, sensitive: bool) -> dict:
    if sensitive:
        payload = encrypt(plaintext.encode(), recipients).decode()
    else:
        payload = plaintext
    return {"encrypted": sensitive, "payload": payload}
```

### Bash (age CLI)

```bash
# Build recipient args from server response
ARGS=()
while IFS= read -r key; do
  ARGS+=(-r "$key")
done <<< "$(curl -s -H "Authorization: Bearer $TOKEN" \
  $SERVICE_URL/clients/public-keys | jq -r '.[].public_key')"

# Encrypt and push
xclip -o | age "${ARGS[@]}" -a | \
  jq -Rsc '{"encrypted": true, "payload": .}' | \
  curl -s -X POST $SERVICE_URL/clipboard \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d @-
```

## Decryption flow (client side)

On receiving an entry:

1. Check `encrypted` flag.
2. If `true` — decrypt using the client's private key.
3. If `false` — use `payload` directly.

### Python (pyrage)

```python
from pyrage import decrypt, x25519

identity = x25519.Identity.from_str(private_key_str)  # load once at startup

def read_entry(entry: dict) -> str:
    if entry["encrypted"]:
        return decrypt(entry["payload"].encode(), [identity]).decode()
    return entry["payload"]
```

### Bash

```bash
age --decrypt -i "$AGE_KEY_FILE" <<< "$PAYLOAD"
```

## Service responsibilities

The service handles only:
- Storing and returning opaque `payload` strings.
- Managing registered clients and their public keys.
- Authenticating push requests via bearer token.
- Exposing `GET /clients/public-keys` — returns list of all registered public keys.

The service **must not** log `payload` contents at any level.

## Library

- Server-side (Python): `pyrage >= 1.2.3` — bindings for the Rust `rage` implementation.
- Client-side (Bash): `age` CLI — reference Go implementation, fully format-compatible.
- Both implementations are interoperable — what `age` CLI encrypts, `pyrage` decrypts
  and vice versa.

## Format compatibility

`age` is a stable, versioned spec (`age-encryption.org/v1`). The armored (ASCII) format
is safe for JSON transport and human-readable logs (though logs should never contain
payload contents — see above).

---

# IPC / Unix Domain Sockets

## Overview

The service exposes two Unix Domain Sockets with distinct roles:

```
/tmp/clipboard.sock        ← HTTP + WebSocket (uvicorn / FastAPI)
/tmp/clipboard_raw.sock    ← JSON-RPC 2.0 over NDJSON (asyncio, lightweight clients)
```

Both sockets run in the same process and share the same application state
(storage, client registry, etc.) via the common asyncio event loop.

The HTTP/WS socket is managed by uvicorn. The raw UDS is started inside
FastAPI's `lifespan` handler so it starts and stops with the application.

It **replaces FIFO** as the low-overhead alternative transport. Like FIFO, it has
no HTTP overhead, but unlike FIFO:
- Supports bidirectional communication (request/response and notifications)
- Uses the same JSON-RPC 2.0 message format as WebSocket for consistency
- Can be used for encrypted clipboard entries with proper key exchange

## HTTP/WS socket — `/tmp/clipboard.sock`

Standard FastAPI endpoints and WebSocket connections. Use for feature-rich clients
that benefit from HTTP semantics (headers, status codes, auth middleware).

**Bash (one-shot via curl):**
```bash
curl --unix-socket /tmp/clipboard.sock \
  -X POST http://localhost/v1/clip.put \
  -H "Content-Type: application/json" \
  -d '{"items": [{"topic": "c", "value": "...", "mime": "text/plain"}]}'
```

**Neovim persistent client via WebSocket** — use a Lua WebSocket library or
the existing `jobstart`/asyncio bridge pattern already in the project.

## Raw UDS socket — `/tmp/clipboard_raw.sock`

Lightweight, zero-overhead transport for simple clients. Uses the same JSON-RPC 2.0
message format as WebSocket (`/ws`), one JSON object per line (`\n` terminated).
Intended for shell scripts, editor integrations, and embedded clients.

### Message format

All messages follow JSON-RPC 2.0 structure (same as WebSocket at `/ws`):

**Request (client → server):**
```json
{"jsonrpc": "2.0", "id": 1, "method": "clip.put", "params": {...}}
{"jsonrpc": "2.0", "id": 2, "method": "clip.watch", "params": {"topics": ["c"]}}
```

**Response (server → client):**
```json
{"jsonrpc": "2.0", "id": 1, "result": {...}}
{"jsonrpc": "2.0", "id": 2, "error": {"code": 1001, "message": "..."}}
```

**Unsolicited event (server → client):**
```json
{"jsonrpc": "2.0", "method": "clip.changed", "params": {"items": [...], "meta": {...}}}
```

Methods supported: `clip.put`, `clip.get`, `clip.watch`, `clip.unwatch`, `topics.list`, `status.get`, `health.get` (same as `/ws`).

### One-shot client (Bash)

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"clip.get","params":{"topic":"c"}}' \
  | socat -t5 - UNIX-CONNECT:/tmp/clipboard_raw.sock
```

Response:
```json
{"jsonrpc": "2.0", "id": 1, "result": {"item": {"topic": "c", "value": "...", ...}}}
```

### One-shot client (Python)

```python
import socket, json

def raw_oneshot(method: str, params: dict) -> dict:
    msg = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect("/tmp/clipboard_raw.sock")
        s.sendall((json.dumps(msg) + "\n").encode())
        response = json.loads(s.makefile().readline())
        return response.get("result") or response.get("error")

# Usage
result = raw_oneshot("clip.get", {"topic": "c"})
print(result["item"]["value"])
```

### Persistent client (Bash with socat)

```bash
{
  echo '{"jsonrpc":"2.0","id":1,"method":"clip.watch","params":{"topics":["c","p"]}}'
  while true; do
    sleep 1
  done
} | socat - UNIX-CONNECT:/tmp/clipboard_raw.sock
```

### Persistent client (Python asyncio)

```python
import asyncio, json

async def raw_persistent():
    reader, writer = await asyncio.open_unix_connection("/tmp/clipboard_raw.sock")
    
    # Subscribe to topics
    watch_msg = {"jsonrpc": "2.0", "id": 1, "method": "clip.watch", "params": {"topics": ["c", "p"]}}
    writer.write((json.dumps(watch_msg) + "\n").encode())
    await writer.drain()
    
    # Receive messages (responses and events)
    while True:
        line = await reader.readuntil(b"\n")
        msg = json.loads(line)
        if "method" in msg:
            print(f"Event: {msg['method']}")
        else:
            print(f"Response: id={msg.get('id')}, result={msg.get('result')}")
```

### Persistent client (Neovim / Lua)

```lua
local client = vim.loop.new_pipe(false)

client:connect("/tmp/clipboard_raw.sock", function(err)
  if err then return end

  -- Send clip.watch request
  local msg = {
    jsonrpc = "2.0",
    id = 1,
    method = "clip.watch",
    params = {topics = {"c", "p"}}
  }
  client:write(vim.json.encode(msg) .. "\n")

  client:read_start(function(err, data)
    if data then
      for line in vim.split(data, "\n", {plain=true}) do
        if line ~= "" then
          local msg = vim.json.decode(line)
          if msg.method == "clip.changed" then
            -- Handle clip change event
          end
        end
      end
    end
  end)
end)
```

## Service responsibilities

- `/tmp/clipboard.sock` — managed entirely by uvicorn, no manual lifecycle needed.
- `/tmp/clipboard_raw.sock` — created and cleaned up in `lifespan`. Always `unlink`
  before binding to avoid `Address already in use` on restart.
- Both sockets process requests through the same `AppState` dispatcher.
- The service **must not** log `payload` contents on either socket.
- Raw UDS has no built-in authentication — use for local/trusted connections or layer
  authentication at the application level.

## Open decisions

- Whether raw socket persistent clients receive entries pushed by other clients
  in real time (pub/sub) or only on explicit pull request.
- Authentication on the raw socket (token in first message vs. none for local-only use).
- Timeout policy for idle raw socket connections.

---

## Open decisions

- `client_id` scheme (UUID vs username vs public key as ID).
- TTL / expiry policy for entries.
- Whether to support key rotation (requires re-encryption of existing entries or
  accepting that old entries become unreadable).
