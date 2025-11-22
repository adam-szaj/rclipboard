#!/usr/bin/env bash
set -euo pipefail

REPO_DIR=${1:-$(pwd)}

UNIT_DIR="$HOME/.config/systemd/user"
ENV_DIR="$HOME/.config/rclipboard"

mkdir -p "$UNIT_DIR" "$ENV_DIR"

copy_unit() {
  local f="$1"
  install -m 0644 "$f" "$UNIT_DIR/"
}

for f in systemd/user/rclipboard.service systemd/user/rclipboard-proxy.service systemd/user/rclipboard.socket systemd/user/rclipboard@.service; do
  if [ -f "$f" ]; then copy_unit "$f"; fi
done

if [ ! -f "$ENV_DIR/env" ]; then
  cp systemd/user/rclipboard.env.example "$ENV_DIR/env"
  echo "Created $ENV_DIR/env (edit as needed)."
fi

mkdir -p "$UNIT_DIR/rclipboard.service.d" "$UNIT_DIR/rclipboard@.service.d" "$UNIT_DIR/rclipboard-proxy.service.d"
cat >"$UNIT_DIR/rclipboard.service.d/override.conf" <<EOF
[Service]
WorkingDirectory=$REPO_DIR
EOF
cat >"$UNIT_DIR/rclipboard@.service.d/override.conf" <<EOF
[Service]
WorkingDirectory=$REPO_DIR
EOF
cat >"$UNIT_DIR/rclipboard-proxy.service.d/override.conf" <<EOF
[Service]
WorkingDirectory=$REPO_DIR
EOF

systemctl --user daemon-reload
echo "Installed user units. Enable with:"
echo "  systemctl --user enable --now rclipboard.service"
echo "or socket-activated:"
echo "  systemctl --user enable --now rclipboard.socket"

