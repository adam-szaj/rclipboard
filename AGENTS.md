# Repository Guidelines

## Project Structure & Module Organization
- `rclipboardd.py`: main aiohttp service (HTTP + WebSocket); CLI flags `--proxy`, `--xsel`.
- `rclipboard_http.py`: REST handlers (`/status`, `/topics`, `/fetch/{topic}`, `/publish/{topic}`).
- `rclipboard_ws.py`: WS fanout, per‑client queues, dispatcher, optional upstream proxy.
- `messages.py`: small JSON/message helpers and timestamps.
- `ClientInterface.py` / `WsClientInterface.py`: protocol helpers, subscribe/unsubscribe, server/client wrappers.
- `XselClientInterface.py`: optional X clipboard updater via `xsel`.
- `rclipctl`: bash helper to publish clipboard data via HTTP.

## Build, Test, and Development Commands
- Create venv + deps: `python3 -m venv .venv && . .venv/bin/activate && pip install -U pip aiohttp websockets watchfiles`
- Run server (auto‑reload): `python rclipboardd.py` (binds `127.0.0.1:8989`).
- Health check: `./rclipctl status --host 127.0.0.1 --port 8989 | jq .` (or `curl -fsS http://127.0.0.1:8989/status | jq .`)
- Publish example (base64 by default): `echo -n 'hello' | ./rctrl-c --encoding base64`.
- Fetch last value: `./rclipctl fetch -c --json --host 127.0.0.1 --port 8989 | jq .` (or `curl -fsS 'http://127.0.0.1:8989/fetch/c?response-type=json' | jq .`)

## Coding Style & Naming Conventions
- Python 3.11+, follow PEP 8; 4‑space indent; limit functions to focused responsibilities.
- Use type hints and docstrings (style matches current modules).
- Naming: modules and functions `snake_case`, classes `CamelCase`, constants `UPPER_SNAKE`.
- Keep public HTTP/WS shapes stable; extend via new fields rather than breaking keys.

## Testing Guidelines
- Framework: prefer `pytest` with `aiohttp` test utilities for HTTP and WS.
- Place tests under `tests/` mirroring module names (e.g., `tests/test_http.py`).
- Cover: status route, publish→dispatcher fanout, fetch 404/200, WS subscribe/ack.
- Run: `pytest -q` (add as you introduce tests).

## Commit & Pull Request Guidelines
- Commits: use Conventional Commits (`feat:`, `fix:`, `refactor:`, `docs:`). Keep scoped, imperative, and concise.
- PRs: include description, rationale, manual test steps (curl/rclipctl), and linked issues. For protocol changes, include example payloads.
- CI/readiness: ensure server starts, `/status` works, and no obvious tracebacks.

## Security & Configuration Tips
- Bind addresses via env or flags: `RCLIPBOARD_BIND_ADDR`, `RCLIPBOARD_BIND_PORT`, `--bind-addr`, `--port`.
- Default bind is localhost; avoid exposing publicly without auth or a reverse proxy.
- Optional X clipboard: requires `xsel`; install via system package manager if needed.
