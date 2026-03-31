#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR=${1:-$(cd "$SCRIPT_DIR/.." && pwd)}

APP_DIR="$HOME/.config/rclipboard"
BIN_DIR="$APP_DIR/bin"
VENV_DIR="$APP_DIR/venv"
SHARE_SYSTEMD_DIR="$APP_DIR/systemd/user"
UNIT_DIR="$HOME/.config/systemd/user"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
SKIP_PIP_INSTALL="${RCLIPBOARD_INSTALL_SKIP_PIP:-0}"

mkdir -p "$UNIT_DIR" "$APP_DIR" "$BIN_DIR" "$SHARE_SYSTEMD_DIR"

copy_unit_template() {
    local src="$1" dst="$2"
    install -m 0644 "$src" "$dst"
}

copy_executable() {
    local src="$1" dst="$2"
    install -m 0755 "$src" "$dst"
}

# ── scripts ──────────────────────────────────────────────────────────────────
for f in "$REPO_DIR"/scripts/*; do
    if [ -f "$f" ]; then
        copy_executable "$f" "$BIN_DIR/$(basename "$f")"
    fi
done
for f in "$REPO_DIR"/scripts/bin/*; do
    if [ -f "$f" ]; then
        copy_executable "$f" "$BIN_DIR/$(basename "$f")"
    fi
done

# ── systemd units ────────────────────────────────────────────────────────────
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

# ── systemd WorkingDirectory overrides ───────────────────────────────────────
mkdir -p \
    "$UNIT_DIR/rclipboard.service.d" \
    "$UNIT_DIR/rclipboard@.service.d" \
    "$UNIT_DIR/rclipboard-proxy.service.d"

for svc in rclipboard.service rclipboard@.service rclipboard-proxy.service; do
    cat > "$UNIT_DIR/${svc}.d/override.conf" << EOF
[Service]
WorkingDirectory=$APP_DIR
EOF
done

"$SYSTEMCTL_BIN" --user daemon-reload

echo
echo "Installed user units, venv, scripts, and config."
echo "  Venv:   $VENV_DIR"
echo "  Bin:    $BIN_DIR"
echo "  Config: $APP_DIR/config.toml"
echo "  Env:    generated at runtime in \$XDG_RUNTIME_DIR/rclipboard/env (via ExecStartPre)"
echo
echo "Add to PATH if needed:"
echo "  export PATH=\"$BIN_DIR:\$PATH\""
echo
echo "Enable with:"
echo "  systemctl --user enable --now rclipboard.service"
echo "or socket-activated:"
echo "  systemctl --user enable --now rclipboard.socket"
