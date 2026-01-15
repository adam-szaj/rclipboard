#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR=${1:-$(cd "$SCRIPT_DIR/.." && pwd)}

APP_DIR="$HOME/.config/rclipboard"
BIN_DIR="$APP_DIR/bin"
VENV_DIR="$APP_DIR/venv"
SHARE_SYSTEMD_DIR="$APP_DIR/systemd/user"
UNIT_DIR="$HOME/.config/systemd/user"
ENV_DIR="$APP_DIR"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
SKIP_PIP_INSTALL="${RCLIPBOARD_INSTALL_SKIP_PIP:-0}"

mkdir -p "$UNIT_DIR" "$ENV_DIR" "$BIN_DIR" "$SHARE_SYSTEMD_DIR"

copy_unit_template() {
  local src="$1"
  local dst="$2"
  install -m 0644 "$src" "$dst"
}

copy_executable() {
  local src="$1"
  local dst="$2"
  install -m 0755 "$src" "$dst"
}

for f in "$REPO_DIR"/scripts/*; do
  if [ -f "$f" ]; then
    copy_executable "$f" "$BIN_DIR/$(basename "$f")"
  fi
done

for f in \
  "$REPO_DIR"/scripts/systemd/user/rclipboard.service \
  "$REPO_DIR"/scripts/systemd/user/rclipboard-proxy.service \
  "$REPO_DIR"/scripts/systemd/user/rclipboard.socket \
  "$REPO_DIR"/scripts/systemd/user/rclipboard@.service
do
  if [ -f "$f" ]; then
    copy_unit_template "$f" "$SHARE_SYSTEMD_DIR/$(basename "$f")"
    copy_unit_template "$f" "$UNIT_DIR/$(basename "$f")"
  fi
done

if [ ! -f "$ENV_DIR/env" ]; then
  cp "$REPO_DIR/scripts/systemd/user/rclipboard.env.example" "$ENV_DIR/env"
  echo "Created $ENV_DIR/env (edit as needed)."
fi

if [ ! -x "$VENV_DIR/bin/python" ]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

if [ "$SKIP_PIP_INSTALL" != "1" ]; then
  "$VENV_DIR/bin/pip" install --upgrade pip
  "$VENV_DIR/bin/pip" install "$REPO_DIR"
fi

mkdir -p "$UNIT_DIR/rclipboard.service.d" "$UNIT_DIR/rclipboard@.service.d" "$UNIT_DIR/rclipboard-proxy.service.d"
cat >"$UNIT_DIR/rclipboard.service.d/override.conf" <<EOF
[Service]
WorkingDirectory=$APP_DIR
EOF
cat >"$UNIT_DIR/rclipboard@.service.d/override.conf" <<EOF
[Service]
WorkingDirectory=$APP_DIR
EOF
cat >"$UNIT_DIR/rclipboard-proxy.service.d/override.conf" <<EOF
[Service]
WorkingDirectory=$APP_DIR
EOF

"$SYSTEMCTL_BIN" --user daemon-reload
echo "Installed user units, venv and scripts."
echo "Venv: $VENV_DIR"
echo "Bin:  $BIN_DIR"
echo "Add to PATH if needed:"
echo "  export PATH=\"$BIN_DIR:\$PATH\""
echo
echo "Enable with:"
echo "  systemctl --user enable --now rclipboard.service"
echo "or socket-activated:"
echo "  systemctl --user enable --now rclipboard.socket"
