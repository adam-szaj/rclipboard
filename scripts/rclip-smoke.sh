#!/usr/bin/env bash
set -euo pipefail

HOST=${HOST:-127.0.0.1}
PORT=${PORT:-8989}

echo "Health:" >&2
./rclipctl health --host "$HOST" --port "$PORT"

echo "Publish (hello -> c):" >&2
echo -n 'hello' | ./rclipctl publish -c --encoding base64 --host "$HOST" --port "$PORT"

echo "Fetch (JSON envelope):" >&2
./rclipctl fetch -c --json --host "$HOST" --port "$PORT" | jq .

echo "Fetch (raw hex):" >&2
./rclipctl fetch -c --encoding hex --host "$HOST" --port "$PORT"

echo >&2 "OK"
