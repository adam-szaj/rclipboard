#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR=${1:-$(cd "$SCRIPT_DIR/.." && pwd)}

APP_DIR="$HOME/.config/rclipboard"
BIN_DIR="$APP_DIR/bin"
VENV_DIR="$APP_DIR/venv"
UNIT_DIR="$HOME/.config/systemd/user"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
SKIP_PIP_INSTALL="${RCLIPBOARD_INSTALL_SKIP_PIP:-0}"

mkdir -p "$UNIT_DIR" "$APP_DIR" "$BIN_DIR"

copy_unit() {
    local src="$1" dst="$2"
    install -m 0644 "$src" "$dst"
}

copy_executable() {
    local src="$1" dst="$2"
    install -m 0755 "$src" "$dst"
}

# ── scripts ──────────────────────────────────────────────────────────────────
for f in "$REPO_DIR"/scripts/*; do
    [ -f "$f" ] && copy_executable "$f" "$BIN_DIR/$(basename "$f")"
done
for f in "$REPO_DIR"/scripts/bin/*; do
    [ -f "$f" ] && copy_executable "$f" "${HOME}/.local/bin/$(basename "$f")"
done
for f in "$REPO_DIR"/scripts/bash.d/*; do
    [ -f "$f" ] && copy_executable "$f" "${HOME}/.bash.d/$(basename "$f")"
done

# ── systemd unit ─────────────────────────────────────────────────────────────
copy_unit "$REPO_DIR/scripts/systemd/user/rclipboard.service" "$UNIT_DIR/rclipboard.service"

mkdir -p "$UNIT_DIR/rclipboard.service.d"
cat > "$UNIT_DIR/rclipboard.service.d/override.conf" << EOF
[Service]
WorkingDirectory=$APP_DIR
EOF

# ── Python venv + package ────────────────────────────────────────────────────
if [ ! -x "$VENV_DIR/bin/python" ]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

if [ "$SKIP_PIP_INSTALL" != "1" ]; then
    "$VENV_DIR/bin/pip" install --upgrade pip --quiet
    "$VENV_DIR/bin/pip" install "$REPO_DIR" --quiet
fi

# ── config.toml (generated once) ─────────────────────────────────────────────
CONF_EXAMPLE="$REPO_DIR/scripts/systemd/user/rclipboard.conf.example"
if [ ! -f "$APP_DIR/config.toml" ] && [ -f "$CONF_EXAMPLE" ]; then
    install -m 0644 "$CONF_EXAMPLE" "$APP_DIR/config.toml"
    echo "Created $APP_DIR/config.toml (edit as needed)."
fi

"$SYSTEMCTL_BIN" --user daemon-reload

echo
echo "Installed: unit, venv, scripts, config."
echo "  Venv:   $VENV_DIR"
echo "  Bin:    $BIN_DIR"
echo "  Config: $APP_DIR/config.toml"
echo
echo "Add to PATH if needed:"
echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
echo
echo "Enable:"
echo "  systemctl --user enable --now rclipboard.service"
