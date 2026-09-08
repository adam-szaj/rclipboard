#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_SOURCE_DIR="$SCRIPT_DIR"

. "$INSTALL_SOURCE_DIR/install/common.sh"

REMOTE=origin
BRANCH=""
TRANSPORT=uds
START_SERVICE=1
PURGE_USER_DATA=0
OPERATION=install
PYTHON_BIN="${PYTHON_BIN:-python3}"
SKIP_PIP_INSTALL="${RCLIPBOARD_INSTALL_SKIP_PIP:-0}"

usage() {
    cat <<'EOF'
Usage: install.sh [OPTIONS]

Install rclipboard as a private per-user service.

Options:
  --remote NAME          Git remote used by updates (default: origin)
  --branch BRANCH        Git branch used by updates (default: current branch)
  --transport uds|tcp    Initial transport (default: uds)
  --no-start             Install without starting the service
  --reset                Recreate managed installation artifacts
  --purge-user-data      With reset/uninstall, remove config and keys after confirmation
  --uninstall            Internal entry used by rclipboard-uninstall
  --update               Internal entry used by rclipboard-update
  -h, --help             Show this help
EOF
}

require_option_value() {
    local option="$1" count="$2"
    [ "$count" -ge 2 ] || die "missing value for $option"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --remote)
            require_option_value "$1" "$#"
            REMOTE="$2"
            shift 2
            ;;
        --branch)
            require_option_value "$1" "$#"
            BRANCH="$2"
            shift 2
            ;;
        --transport)
            require_option_value "$1" "$#"
            TRANSPORT="$2"
            shift 2
            ;;
        --no-start)
            START_SERVICE=0
            shift
            ;;
        --reset)
            [ "$OPERATION" = install ] \
                || die "only one lifecycle operation may be selected"
            OPERATION=reset
            shift
            ;;
        --purge-user-data)
            PURGE_USER_DATA=1
            shift
            ;;
        --uninstall)
            [ "$OPERATION" = install ] \
                || die "only one lifecycle operation may be selected"
            OPERATION=uninstall
            shift
            ;;
        --update)
            [ "$OPERATION" = install ] \
                || die "only one lifecycle operation may be selected"
            OPERATION=update
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *) die "unknown argument: $1" ;;
    esac
done

case "$TRANSPORT" in
    uds|tcp) ;;
    *) die "transport must be uds or tcp" ;;
esac
if [ "$PURGE_USER_DATA" -eq 1 ] \
    && [ "$OPERATION" != reset ] \
    && [ "$OPERATION" != uninstall ]; then
    die "--purge-user-data requires --reset or --uninstall"
fi

case "$OPERATION" in
    install) ;;
    reset|uninstall|update)
        die "$OPERATION is not available until its lifecycle implementation is installed"
        ;;
esac

REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_DIR="$(resolve_app_dir)"
CONFIG_DIR="$APP_DIR"
BIN_DIR="$APP_DIR/bin"
VENV_DIR="$APP_DIR/venv"
INSTALLER_DIR="$APP_DIR/installer"
METADATA_FILE="$APP_DIR/install.conf"
CONFIG_FILE="$APP_DIR/config.toml"

require_python_311
require_git_checkout
require_single_line_value repo_dir "$REPO_DIR"
CURRENT_BRANCH="$(current_git_branch)"
if [ -z "$BRANCH" ]; then
    BRANCH="$CURRENT_BRANCH"
fi
require_single_line_value remote "$REMOTE"
require_single_line_value branch "$BRANCH"
require_git_target
require_install_sources

PLATFORM="${RCLIPBOARD_INSTALL_UNAME:-$(uname -s)}"
case "$PLATFORM" in
    Linux) . "$INSTALL_SOURCE_DIR/install/systemd.sh" ;;
    Darwin) . "$INSTALL_SOURCE_DIR/install/launchd.sh" ;;
    *) die "unsupported operating system: $PLATFORM" ;;
esac

service_preflight || die "service preflight failed"

umask 077
mkdir -p "$APP_DIR" "$BIN_DIR" "$INSTALLER_DIR"
chmod 0700 "$APP_DIR" "$BIN_DIR" "$INSTALLER_DIR"

if [ ! -x "$VENV_DIR/bin/python" ]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR" \
        || die "failed to create Python virtual environment"
fi

if [ "$SKIP_PIP_INSTALL" != 1 ]; then
    "$VENV_DIR/bin/pip" install "$REPO_DIR" --quiet \
        || die "failed to install rclipboard package"
fi

install_public_scripts || die "failed to install public commands"
install_installer_payload || die "failed to install lifecycle payload"

if [ ! -e "$CONFIG_FILE" ] && [ ! -L "$CONFIG_FILE" ]; then
    config_temporary="${CONFIG_FILE}.tmp.$$"
    install -m 0600 \
        "$INSTALL_SOURCE_DIR/config/rclipboard.conf.example" \
        "$config_temporary" || die "failed to create initial configuration"
    if [ "$TRANSPORT" = tcp ]; then
        "$PYTHON_BIN" - "$config_temporary" <<'PY' \
            || die "failed to configure TCP transport"
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
replacements = {
    'endpoint = "uds://${XDG_RUNTIME_DIR}/rclipboard/uds.sock"':
        'endpoint = "127.0.0.1:8989"',
    'transport = "uds"': 'transport = "tcp"',
    '# endpoint = ""': 'endpoint = "127.0.0.1:8989"',
}
for old, new in replacements.items():
    if text.count(old) != 1:
        raise SystemExit(f"configuration template mismatch: {old}")
    text = text.replace(old, new)
path.write_text(text)
PY
    fi
    chmod 0600 "$config_temporary"
    mv -f "$config_temporary" "$CONFIG_FILE"
    printf 'Created %s (edit as needed).\n' "$CONFIG_FILE"
fi

service_install || die "failed to install $SERVICE_PLATFORM service"
write_install_metadata "$METADATA_FILE" \
    "$REPO_DIR" "$REMOTE" "$BRANCH" \
    || die "failed to write installation metadata"

if [ "$START_SERVICE" -eq 1 ]; then
    service_start || die "failed to start $SERVICE_PLATFORM service"
fi

printf '\nInstalled rclipboard user service.\n'
printf '  Platform: %s\n' "$SERVICE_PLATFORM"
printf '  Venv:     %s\n' "$VENV_DIR"
printf '  Bin:      %s\n' "$BIN_DIR"
printf '  Config:   %s\n' "$CONFIG_FILE"
service_print_status_hint
