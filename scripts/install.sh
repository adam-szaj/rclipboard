#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_SOURCE_DIR="$SCRIPT_DIR"

. "$INSTALL_SOURCE_DIR/install/common.sh"

REMOTE=origin
BRANCH=""
REMOTE_WAS_SET=0
BRANCH_WAS_SET=0
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
            REMOTE_WAS_SET=1
            shift 2
            ;;
        --branch)
            require_option_value "$1" "$#"
            BRANCH="$2"
            BRANCH_WAS_SET=1
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
        --internal-refresh)
            [ "$OPERATION" = install ] \
                || die "only one lifecycle operation may be selected"
            OPERATION=internal-refresh
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
    install|reset|uninstall|update|internal-refresh) ;;
esac

APP_DIR="$(resolve_app_dir)"
CONFIG_DIR="$APP_DIR"
BIN_DIR="$APP_DIR/bin"
VENV_DIR="$APP_DIR/venv"
INSTALLER_DIR="$APP_DIR/installer"
METADATA_FILE="$APP_DIR/install.conf"
CONFIG_FILE="$APP_DIR/config.toml"

if [ "$OPERATION" = update ]; then
    requested_remote="$REMOTE"
    requested_branch="$BRANCH"
    validate_app_dir_for_cleanup
    read_install_metadata "$METADATA_FILE"
    if [ "$REMOTE_WAS_SET" -eq 1 ]; then
        REMOTE="$requested_remote"
    fi
    if [ "$BRANCH_WAS_SET" -eq 1 ]; then
        BRANCH="$requested_branch"
    fi
    perform_update
    exit 0
fi

if [ "$OPERATION" = uninstall ]; then
    validate_app_dir_for_cleanup
    read_install_metadata "$METADATA_FILE"
    case "$REPO_DIR" in
        /*) ;;
        *) die "invalid repository path in installation metadata: $REPO_DIR" ;;
    esac

    PLATFORM="${RCLIPBOARD_INSTALL_UNAME:-$(uname -s)}"
    case "$PLATFORM" in
        Linux) . "$INSTALL_SOURCE_DIR/install/systemd.sh" ;;
        Darwin) . "$INSTALL_SOURCE_DIR/install/launchd.sh" ;;
        *) die "unsupported operating system: $PLATFORM" ;;
    esac

    if [ "$PURGE_USER_DATA" -eq 1 ]; then
        confirm_user_data_purge
    fi
    service_uninstall || die "failed to uninstall $SERVICE_PLATFORM service"
    remove_managed_runtime || die "failed to remove managed runtime"
    if [ "$PURGE_USER_DATA" -eq 1 ]; then
        purge_user_data || die "failed to purge user data"
    fi
    remove_empty_app_dir
    printf 'Uninstalled rclipboard user service.\n'
    exit 0
fi

SOURCE_REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)"
if [ "$OPERATION" = internal-refresh ]; then
    requested_remote="$REMOTE"
    requested_branch="$BRANCH"
    validate_app_dir_for_cleanup
    read_install_metadata "$METADATA_FILE"
    RECORDED_REPO_DIR="$REPO_DIR"
    [ "$RECORDED_REPO_DIR" = "$SOURCE_REPO_DIR" ] \
        || die "internal refresh does not match the recorded source checkout"
    if [ "$REMOTE_WAS_SET" -eq 1 ]; then
        REMOTE="$requested_remote"
    fi
    if [ "$BRANCH_WAS_SET" -eq 1 ]; then
        BRANCH="$requested_branch"
    fi
    REPO_DIR="$SOURCE_REPO_DIR"
    require_update_checkout
else
    REPO_DIR="$SOURCE_REPO_DIR"
fi
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

if [ "$OPERATION" = reset ]; then
    validate_app_dir_for_cleanup
    if [ "$PURGE_USER_DATA" -eq 1 ]; then
        confirm_user_data_purge
    fi
    service_uninstall || die "failed to uninstall $SERVICE_PLATFORM service"
    remove_managed_runtime || die "failed to remove managed runtime"
    if [ "$PURGE_USER_DATA" -eq 1 ]; then
        purge_user_data || die "failed to purge user data"
    fi
fi

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
if [ "$OPERATION" = internal-refresh ]; then
    service_restart || die "failed to restart $SERVICE_PLATFORM service"
    write_install_metadata "$METADATA_FILE" \
        "$REPO_DIR" "$REMOTE" "$BRANCH" \
        || die "failed to write installation metadata"
else
    write_install_metadata "$METADATA_FILE" \
        "$REPO_DIR" "$REMOTE" "$BRANCH" \
        || die "failed to write installation metadata"
    if [ "$START_SERVICE" -eq 1 ]; then
        service_start || die "failed to start $SERVICE_PLATFORM service"
    fi
fi

printf '\nInstalled rclipboard user service.\n'
printf '  Platform: %s\n' "$SERVICE_PLATFORM"
printf '  Venv:     %s\n' "$VENV_DIR"
printf '  Bin:      %s\n' "$BIN_DIR"
printf '  Config:   %s\n' "$CONFIG_FILE"
service_print_status_hint
