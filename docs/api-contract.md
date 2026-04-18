# rclipboard API Contract

## Goal

Shared application data structures across:

- WebSocket (`/ws`)
- HTTP REST (`/v1/…`)
- Raw Unix Domain Socket (NDJSON)
- Proxy hops

The transport may change framing, but not the shape of operation inputs and outputs.

## Core Rule

The canonical contract is:

- operation name: `method`
- input model: `params`
- success model: `result`
- failure model: `error`

WebSocket and raw UDS use full JSON-RPC 2.0 envelopes.
HTTP maps one endpoint to one `method`; request and response bodies carry only
`params`, `result`, or `error`.

---

## Shared Data Models

### `ClipboardItem`

```json
{
  "topic": "c",
  "mime": "text/plain",
  "encoding": "utf-8",
  "value": "hello",
  "encrypted": false,
  "size": 5,
  "digest": {
    "algo": "sha256",
    "value": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
  }
}
```

Required: `topic`, `value`

Optional: `mime`, `encoding`, `encrypted`, `size`, `digest`

#### Encoding rules

| Payload kind | `mime`                     | `encoding` | `value`          |
|--------------|----------------------------|------------|------------------|
| text         | `text/plain`               | `utf-8`    | JSON string      |
| binary       | `application/octet-stream` | `base64`   | base64 string    |
| structured   | `application/json`         | `json`     | JSON object/array|

#### Encrypted items

When `encrypted: true`:
- `value` contains base64-encoded [age](https://age-encryption.org/) ciphertext
- `mime` is typically `application/octet-stream`, `encoding` `base64`
- HTTP `clip.get` requires the `X-Age-Public-Key: age1…` request header with a
  key registered in the server registry; returns `403 4032` if missing or unknown
- `rclipctl get` auto-decrypts using the local private key (`~/.config/rclipboard/age_key.txt`)

### `TopicData` (internal / proxy wire format)

```json
{
  "topic": "c",
  "value": { "value": "aGVsbG8=", "type": "binary", "encoding": "base64" },
  "meta":  { "app": "tmux", "ts": "2026-04-17T10:00:00.000000+00:00" },
  "stub":  false,
  "fetch_url": null
}
```

`stub: true` is set when the server omits the value because the payload exceeds
the lazy-sync threshold (`RCLIPBOARD_LAZY_LOCAL_KB`).  Clients should fetch the
full value from `fetch_url` (`GET /v1/clip/{topic}`) on demand.

### `TopicStatus`

```json
{
  "topic": "c",
  "ts": "2026-04-17T10:00:00.000000+00:00",
  "stub": false,
  "size": 1024,
  "stored_ago": 3.142
}
```

---

## Method Set

| Method         | HTTP endpoint              | Direction      |
|----------------|----------------------------|----------------|
| `clip.put`     | `POST /v1/clip.put`        | client → server|
| `clip.get`     | `POST /v1/clip.get`        | client → server|
| `clip.watch`   | WS / UDS only              | client → server|
| `clip.unwatch` | WS / UDS only              | client → server|
| `clip.changed` | WS / UDS only              | server → client (notification) |
| `topics.list`  | `POST /v1/topics.list`     | client → server|
| `health.get`   | `GET /v1/health.get`       | client → server|
| `status.get`   | `GET /v1/status.get`       | client → server|

---

## JSON-RPC 2.0 over WebSocket / Raw UDS

Both `/ws` (WebSocket) and `/tmp/clipboard_raw.sock` (raw UDS, NDJSON) use
identical JSON-RPC 2.0 framing.  UDS uses `\n`-terminated single-line JSON.

### Request (client → server)

```json
{
  "jsonrpc": "2.0",
  "id": "req-1",
  "method": "clip.put",
  "params": {
    "items": [
      { "topic": "c", "mime": "text/plain", "encoding": "utf-8", "value": "hello" }
    ],
    "meta": { "app": "tmux" }
  }
}
```

A notification (no `id`) is a fire-and-forget request that never receives a response.

### Success response

```json
{
  "jsonrpc": "2.0",
  "id": "req-1",
  "result": {
    "items": [
      { "topic": "c", "mime": "text/plain", "encoding": "utf-8", "value": "hello" }
    ]
  }
}
```

### Error response

```json
{
  "jsonrpc": "2.0",
  "id": "req-1",
  "error": { "code": 1001, "message": "Topic not found", "data": { "topic": "c" } }
}
```

### Server-push event (`clip.changed`)

```json
{
  "jsonrpc": "2.0",
  "method": "clip.changed",
  "params": {
    "items": [
      { "topic": "c", "mime": "text/plain", "encoding": "utf-8", "value": "new text",
        "stub": false }
    ],
    "meta": { "app": "xsel", "ts": "2026-04-17T10:00:00.000000+00:00" }
  }
}
```

When `stub: true` in an item, `value` is empty and the client should call
`GET /v1/clip/{topic}` to retrieve the full payload.

---

## HTTP Mapping

| Method        | HTTP endpoint            | Auth header required |
|---------------|--------------------------|----------------------|
| `clip.put`    | `POST /v1/clip.put`      | —                    |
| `clip.get`    | `POST /v1/clip.get`      | `X-Age-Public-Key` when encrypted |
| `topics.list` | `POST /v1/topics.list`   | —                    |
| `health.get`  | `GET  /v1/health.get`    | —                    |
| `status.get`  | `GET  /v1/status.get`    | —                    |
| `keys.publish`| `POST /v1/keys.publish`  | `Authorization: Bearer <ADMIN_TOKEN>` |
| `keys.list`   | `GET  /v1/keys.list`     | —                    |
| `proxy.connect`    | `POST /v1/proxy.connect`    | `Authorization: Bearer <ADMIN_TOKEN>` |
| `proxy.disconnect` | `POST /v1/proxy.disconnect` | `Authorization: Bearer <ADMIN_TOKEN>` |

Rules:
- request body = `params` object for that method
- success body = `result` object
- error body = `error` object (same shape as JSON-RPC error)
- HTTP status code carries transport-level meaning:
  `200` success, `400` bad params, `403` forbidden, `404` not found,
  `503` feature not available

---

## Method Contracts

### `clip.put`

Params:
```json
{
  "items": [
    { "topic": "c", "mime": "application/octet-stream",
      "encoding": "base64", "value": "aGVsbG8=", "encrypted": false }
  ],
  "meta": { "app": "tmux" }
}
```

Result:
```json
{ "items": [ { "topic": "c", "mime": "application/octet-stream",
               "encoding": "base64", "value": "aGVsbG8=" } ] }
```

### `clip.get`

Params:
```json
{ "topic": "c" }
```

Result:
```json
{ "item": { "topic": "c", "mime": "text/plain", "encoding": "utf-8",
            "value": "hello", "encrypted": false } }
```

Error `1001` when topic not found.
Error `403 / 4032` when item is encrypted and the public key header is missing or
not registered.

### `clip.watch`

Params:
```json
{ "topics": ["c", "p"], "public_key": "age1..." }
```

`public_key` is optional; when provided, the connection will receive
`clip.changed` for encrypted topics whose key matches the registry.

Result:
```json
{
  "topics": ["c", "p"],
  "contents": {
    "c": { "topic": "c", "value": {...}, "meta": {...}, "stub": false }
  }
}
```

`contents` carries current values for topics that already have data.
Items may have `stub: true` when `RCLIPBOARD_LAZY_LOCAL_KB` is set and the
payload exceeds the threshold.

### `clip.unwatch`

Params: `{ "topics": ["c"] }`
Result: `{ "topics": ["c"] }`

### `topics.list`

Params: `{}`
Result: `{ "topics": ["c", "p", "vim"] }`

### `health.get`

Params: `{}`
Result:
```json
{
  "ok": true,
  "xsel_enabled": false,
  "xsel_good": false,
  "proxy_enabled": false,
  "proxy_good": false
}
```

### `status.get`

Params: `{}`
Result:
```json
{
  "ok": true,
  "topics": ["c", "p"],
  "topic_status": [
    { "topic": "c", "ts": "2026-04-17T10:00:00+00:00",
      "stub": false, "size": 5, "stored_ago": 1.234 }
  ],
  "clients": ["WSServerConnection[ws://…]"],
  "xsel":  { "enabled": false, "good": false },
  "proxy": {
    "enabled": true,
    "good": true,
    "endpoint": "127.0.0.1:8989",
    "connected": true,
    "connect_count": 1,
    "rx_count": 42,
    "tx_count": 7,
    "last_rx_ago": 0.3,
    "last_tx_ago": 1.1,
    "last_connect_ago": 120.5,
    "last_disconnect_ago": null,
    "last_error": null,
    "reconnect": true
  }
}
```

### `keys.publish`

HTTP only. Requires `Authorization: Bearer <RCLIPBOARD_ADMIN_TOKEN>`.
Returns `503 5031` when `RCLIPBOARD_ADMIN_TOKEN` is not configured.

Body: `{ "public_key": "age1...", "label": "laptop" }`
Result: `{ "ok": true, "key_id": "a1b2c3d4e5f6a7b8" }`

### `keys.list`

HTTP only.
Result: `{ "keys": [ { "key_id": "…", "public_key": "age1…", "label": "laptop" } ] }`

### `proxy.connect`

HTTP only. Requires `Authorization: Bearer <RCLIPBOARD_ADMIN_TOKEN>`.

Disconnects any existing upstream and connects a new one.

Body: `{ "endpoint": "host:port", "reconnect": true }`
Result: `{ "ok": true, "endpoint": "host:port" }`

`endpoint` accepts the same formats as `RCLIPBOARD_UPSTREAM_ENDPOINT`:
`host:port`, `http://host:port`, `uds:///path/to.sock`.

### `proxy.disconnect`

HTTP only. Requires `Authorization: Bearer <RCLIPBOARD_ADMIN_TOKEN>`.

Disconnects the upstream and sets `reconnect=false` (no auto-reconnect).

Body: `{}`
Result: `{ "ok": true }`

---

## Error Contract

```json
{ "code": 1002, "message": "Invalid encoding", "data": { "field": "items[0].encoding" } }
```

Application error codes:

| Code | Meaning                       |
|------|-------------------------------|
| 1000 | Invalid / malformed request   |
| 1001 | Topic not found               |
| 1002 | Invalid encoding              |
| 1003 | Unsupported method            |
| 1004 | Upstream unavailable          |
| 1005 | Clipboard backend unavailable |
| 4031 | Forbidden (wrong token)       |
| 4032 | Not registered (encrypted get without valid key) |
| 5031 | Feature not enabled (key registry / admin token not configured) |

---

## Lazy Sync Protocol

When `RCLIPBOARD_LAZY_LOCAL_KB > 0`, the server may replace large payloads with
stubs in `clip.changed` notifications and `clip.watch` initial contents.

A stub item:
```json
{ "topic": "large", "stub": true, "fetch_url": "/v1/clip/large",
  "value": { "value": "", "type": "text", "encoding": "plain" },
  "meta": { "ts": "…" } }
```

Client behaviour when a stub is received:
1. Display placeholder / "large payload" indicator
2. Fetch full value on demand: `GET /v1/clip/{topic}`
3. Cache locally

The `GET /v1/clip/{topic}` endpoint always returns the full `TopicData`.

---

## Proxy Mode

When `RCLIPBOARD_PROXY=1` (or connected dynamically via `proxy.connect`),
a `ProxyClient` subscribes to the upstream with `clip.watch` and replicates
topics locally.  The proxy also accepts JSON-RPC requests from upstream
(symmetric peer), so upstream can call `clip.get` / `clip.put` against the proxy
over the same WebSocket connection.

Proxy telemetry is exposed via `status.get` under the `proxy` key (see above).

### Environment variables

| Variable                        | Default   | Purpose |
|---------------------------------|-----------|---------|
| `RCLIPBOARD_PROXY`              | `0`       | `1` to enable proxy mode at startup |
| `RCLIPBOARD_UPSTREAM_ENDPOINT`  | —         | Upstream address (TCP / UDS / WS URL) |
| `RCLIPBOARD_LAZY_UPSTREAM_KB`   | `0`       | Skip upstream put for payloads ≥ N KB (0 = disabled) |
| `RCLIPBOARD_LAZY_LOCAL_KB`      | `0`       | Send stub to WS/UDS clients for payloads ≥ N KB (0 = disabled) |
| `RCLIPBOARD_UPSTREAM_PUT_MODE`  | `immediate` | `immediate` / `debounced` / `on_demand` |
| `RCLIPBOARD_UPSTREAM_SYNC_DELAY_MS` | `5000` | Debounce delay for `debounced` put mode |

---

## CLI (`rclipctl`)

Auto-detects transport: FIFO → UDS → TCP.

| Subcommand | Purpose |
|------------|---------|
| `put` / `clip` | Read stdin, call `clip.put` |
| `get`           | Call `clip.get`, auto-decrypt if `encrypted=true` |
| `exec --encrypt-output -- <cmd>` | Run cmd, encrypt stdout with age, print to stdout |
| `exec --decrypt-input -- <cmd>`  | Decrypt stdin, feed plaintext to cmd |
| `keygen`        | Generate age X25519 keypair |
| `register`      | Register own public key with server |
| `keys-list`     | List registered keys |
| `status` / `topics` / `health` | Server info |

Encryption options for `put`:
`--encrypt` / `-E`, `--key <age1…>`, `--key-file <path>`, `--fetch-keys`

## `rcliptunel`

Establishes SSH port-forward for rclipboard and optionally manages the proxy
connection lifecycle.

```
rcliptunel --ssh user@host [--reverse|--forward]
           [--local <endpoint>] [--remote <endpoint>]
           [--proxy-auto | --proxy-connect | --proxy-disconnect]
           [--token <admin_token>] [--no-proxy-reconnect]
           [--daemonize] [--login]
```

With `--proxy-auto`:
- sends `proxy.connect` (pointing at the tunnel endpoint) after SSH is up
- registers `trap EXIT` to send `proxy.disconnect` on Ctrl+C / session end

---

## File / NDJSON Mapping

Two valid interchange formats:

- full JSON-RPC 2.0 document (single message)
- NDJSON stream (one JSON-RPC message per line)

Both `params`, `result`, and `error` objects are usable standalone for:
debugging, replay, proxy queues, test fixtures, offline import/export.
