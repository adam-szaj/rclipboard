#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR=${1:-$(cd "$SCRIPT_DIR/.." && pwd)}

APP_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/rclipboard"
BIN_DIR="$APP_DIR/bin"
VENV_DIR="$APP_DIR/venv"
CONFIG_DIR="$APP_DIR"
START_SERVICE=0
PYTHON_BIN="${PYTHON_BIN:-python3}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
SKIP_PIP_INSTALL="${RCLIPBOARD_INSTALL_SKIP_PIP:-0}"

. "$REPO_DIR/scripts/install/common.sh"
. "$REPO_DIR/scripts/install/systemd.sh"

mkdir -p "$APP_DIR" "$BIN_DIR"

copy_executable() {
    local src="$1" dst="$2"
    install -m 0755 "$src" "$dst"
}

# ── scripts ──────────────────────────────────────────────────────────────────
for f in "$REPO_DIR"/scripts/*; do
    [ -f "$f" ] && copy_executable "$f" "${BIN_DIR}/$(basename "$f")"
done
for f in "$REPO_DIR"/scripts/bin/*; do
    [ -f "$f" ] && copy_executable "$f" "${BIN_DIR}/$(basename "$f")"
done
mkdir -p "${HOME}/.bash.d"
for f in "$REPO_DIR"/scripts/bash.d/*; do
    [ -f "$f" ] && copy_executable "$f" "${HOME}/.bash.d/$(basename "$f")"
done

# ── systemd unit ─────────────────────────────────────────────────────────────
service_install

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
echo
echo "For X11/xsel clipboard sync, also enable the session-bound display"
echo "publisher (no-op on headless / non-graphical logins):"
echo "  systemctl --user enable --now rclipboard-display.service"
