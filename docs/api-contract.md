# rclipboard API Contract

## Goal

Shared application data structures across:

- WebSocket
- HTTP
- file export/import
- proxy hops

The transport may change framing, but not the shape of operation inputs and outputs.

## Core Rule

The canonical contract is:

- operation name: `method`
- input model: `params`
- success model: `result`
- failure model: `error`

WebSocket uses full JSON-RPC 2.0 envelopes.
HTTP maps one endpoint to one `method`, while request and response bodies carry only `params`, `result`, or `error`.

## Method Set

Initial method set:

- `clip.put`
- `clip.get`
- `clip.watch`
- `clip.unwatch`
- `topics.list`
- `health.get`
- `status.get`

## Shared Data Models

### `ClipboardItem`

```json
{
  "topic": "c",
  "mime": "text/plain",
  "encoding": "utf-8",
  "value": "hello",
  "size": 5,
  "digest": {
    "algo": "sha256",
    "value": "2cf24dba5fb0..."
  }
}
```

Required:

- `topic`
- `value`

Recommended:

- `mime`
- `encoding`
- `size`
- `digest`

### Encoding Rules

- text payload:
  - `mime`: `text/plain`
  - `encoding`: `utf-8`
  - `value`: JSON string
- binary payload:
  - `mime`: `application/octet-stream`
  - `encoding`: `base64`
  - `value`: base64 string
- structured payload:
  - `mime`: application-specific JSON type
  - `encoding`: `json`
  - `value`: JSON object or array

## JSON-RPC 2.0 over WebSocket

### Request

```json
{
  "jsonrpc": "2.0",
  "id": "req-1",
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
      "app": "tmux"
    }
  }
}
```

### Success Response

```json
{
  "jsonrpc": "2.0",
  "id": "req-1",
  "result": {
    "items": [
      {
        "topic": "c",
        "mime": "text/plain",
        "encoding": "utf-8",
        "value": "hello"
      }
    ]
  }
}
```

### Error Response

```json
{
  "jsonrpc": "2.0",
  "id": "req-1",
  "error": {
    "code": 1001,
    "message": "Topic not found",
    "data": {
      "topic": "c"
    }
  }
}
```

### Notifications and Events

For asynchronous server pushes over WebSocket:

- client notification: JSON-RPC request without `id`
- server event: JSON-RPC request without `id`

Example server event:

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
        "value": "new text"
      }
    ],
    "meta": {
      "app": "xsel"
    }
  }
}
```

## HTTP Mapping

HTTP endpoint names correspond to WebSocket `method` names.

Canonical mapping:

- `POST /v1/clip.put`
- `POST /v1/clip.get`
- `POST /v1/clip.watch`
- `POST /v1/clip.unwatch`
- `POST /v1/topics.list`
- `GET /v1/health.get`
- `GET /v1/status.get`

Rules:

- request body = exactly the `params` object for that method
- success body = exactly the `result` object for that method
- error body = exactly the JSON-RPC `error` object

HTTP status code still carries transport-level meaning:

- `200` for success
- `400` for invalid params / contract violation
- `404` for not found
- `409` for state conflict
- `500` for internal failure

Example:

`POST /v1/clip.put`

```json
{
  "items": [
    {
      "topic": "c",
      "mime": "application/octet-stream",
      "encoding": "base64",
      "value": "aGVsbG8="
    }
  ],
  "meta": {
    "app": "tmux"
  }
}
```

Success response:

```json
{
  "items": [
    {
      "topic": "c",
      "mime": "application/octet-stream",
      "encoding": "base64",
      "value": "aGVsbG8="
    }
  ]
}
```

Error response body:

```json
{
  "code": 1001,
  "message": "Topic not found",
  "data": {
    "topic": "c"
  }
}
```

## File Mapping

Two valid file formats:

- full JSON-RPC messages, one JSON document
- NDJSON stream of JSON-RPC messages

For pure storage or interchange of payloads, files may also store only:

- `params`
- `result`
- `error`

That means the same structures are reusable for:

- debugging
- replay
- proxy queues
- fixtures
- offline import/export

## Method Contracts

### `clip.put`

Params:

```json
{
  "items": [
    {
      "topic": "c",
      "mime": "application/octet-stream",
      "encoding": "base64",
      "value": "aGVsbG8="
    }
  ],
  "meta": {
    "app": "tmux"
  }
}
```

Result:

```json
{
  "items": [
    {
      "topic": "c",
      "mime": "application/octet-stream",
      "encoding": "base64",
      "value": "aGVsbG8="
    }
  ]
}
```

### `clip.get`

Params:

```json
{
  "topic": "c"
}
```

Result:

```json
{
  "item": {
    "topic": "c",
    "mime": "text/plain",
    "encoding": "utf-8",
    "value": "hello"
  }
}
```

### `clip.watch`

Params:

```json
{
  "topics": ["c", "p"]
}
```

Result:

```json
{
  "topics": ["c", "p"]
}
```

### `topics.list`

Params:

```json
{}
```

Result:

```json
{
  "topics": ["c", "p", "vim"]
}
```

### `health.get`

Params:

```json
{}
```

Result:

```json
{
  "ok": true
}
```

## Error Contract

Shared error object:

```json
{
  "code": 1002,
  "message": "Invalid encoding",
  "data": {
    "field": "items[0].encoding"
  }
}
```

Suggested application codes:

- `1000` invalid request
- `1001` topic not found
- `1002` invalid encoding
- `1003` unsupported method
- `1004` upstream unavailable
- `1005` clipboard backend unavailable

## CLI Mapping

Recommended commands:

- `rclipctl put`
- `rclipctl get`
- `rclipctl watch`
- `rclipctl topics`
- `rclipctl health`
- `rclipctl status`
- `rclipctl rpc`

Behavior:

- `put`, `get`, `watch`, `topics`, `health`, `status` build `params`
- over WS, CLI wraps them into JSON-RPC 2.0
- over HTTP, CLI sends them directly to `/v1/<method>`
- `rpc` is a debug command for sending full JSON-RPC payloads as-is

## Design Decisions

- JSON-RPC 2.0 is the canonical WebSocket contract.
- HTTP is not forced to carry full JSON-RPC envelopes.
- The shared contract lives at `params`, `result`, and `error`.
- Transport-specific wrappers should stay thin.

## Immediate Refactor Target

The codebase should converge to shared internal models for:

- `ClipboardItem`
- `ClipPutParams`
- `ClipPutResult`
- `ClipGetParams`
- `ClipGetResult`
- `TopicsListParams`
- `TopicsListResult`
- `HealthResult`
- `StatusResult`
- `RPCError`

HTTP handlers, WS handlers, proxy, and CLI should all depend on those models.
