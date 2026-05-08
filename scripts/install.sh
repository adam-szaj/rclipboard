#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR=${1:-$(cd "$SCRIPT_DIR/.." && pwd)}

APP_DIR="$HOME/.config/rclipboard"
BIN_DIR="$APP_DIR/bin"
VENV_DIR="$APP_DIR/venv"
USER_BIN="$HOME/bin"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SKIP_PIP_INSTALL="${RCLIPBOARD_INSTALL_SKIP_PIP:-0}"

mkdir -p "$APP_DIR" "$BIN_DIR" "$USER_BIN" "$HOME/.bash.d"

copy_executable() {
    local src="$1" dst="$2"
    install -m 0755 "$src" "$dst"
}

copy_file() {
    local src="$1" dst="$2"
    install -m 0644 "$src" "$dst"
}

# ── scripts ──────────────────────────────────────────────────────────────────
for f in "$REPO_DIR"/scripts/*; do
    [ -f "$f" ] && copy_executable "$f" "${BIN_DIR}/$(basename "$f")"
done
for f in "$REPO_DIR"/scripts/bin/*; do
    [ -f "$f" ] && copy_executable "$f" "${BIN_DIR}/$(basename "$f")"
done
for f in "$REPO_DIR"/scripts/bash.d/*; do
    [ -f "$f" ] && copy_executable "$f" "${HOME}/.bash.d/$(basename "$f")"
done

# ── ~/bin shortcuts ───────────────────────────────────────────────────────────
for f in rclipctl rcliptunel; do
    [ -f "$REPO_DIR/scripts/bin/$f" ] && copy_executable "$REPO_DIR/scripts/bin/$f" "$USER_BIN/$f"
done

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
    copy_file "$CONF_EXAMPLE" "$APP_DIR/config.toml"
    echo "Created $APP_DIR/config.toml (edit as needed)."
fi

echo
echo "Installed: venv, scripts, config."
echo "  Venv:   $VENV_DIR"
echo "  Bin:    $BIN_DIR  (also $USER_BIN)"
echo "  Config: $APP_DIR/config.toml"
echo
echo "Start the server:"
echo "  $VENV_DIR/bin/rclipboard"
echo
echo "Run on login (pick one):"
echo "  a) cron @reboot:  add to crontab -e:"
echo "       @reboot $VENV_DIR/bin/rclipboard"
echo "  b) ~/.bashrc nohup wrapper:"
echo '       if ! pgrep -x rclipboard >/dev/null; then'
echo "           nohup $VENV_DIR/bin/rclipboard >/tmp/rclipboard.log 2>&1 &"
echo '       fi'
echo
echo "Configure interactively:"
echo "  $BIN_DIR/rclipboard-setup"
echo
echo "Add ~/bin to PATH if needed:"
echo '  export PATH="$HOME/bin:$PATH"'
