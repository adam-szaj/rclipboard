#!/usr/bin/env bash
set -euo pipefail

THIS_DIR="$(cd "$(dirname "$0")" && pwd)"
HOST=${HOST:-127.0.0.1}
PORT=${PORT:-8989}

echo "Health:" >&2
"$THIS_DIR/rclipctl" health --host "$HOST" --port "$PORT"

echo "Publish (hello -> c):" >&2
echo -n 'hello' | "$THIS_DIR/rclipctl" put -c --host "$HOST" --port "$PORT"

echo "Fetch (JSON result):" >&2
"$THIS_DIR/rclipctl" get -c --json --host "$HOST" --port "$PORT" | jq .

echo "Fetch (raw hex):" >&2
"$THIS_DIR/rclipctl" get -c --encoding hex --host "$HOST" --port "$PORT"

echo >&2 "OK"
