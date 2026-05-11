#!/usr/bin/env bash
# scripts/deploy.sh — push-based multi-host rclipboard deployer
# Usage: ./scripts/deploy.sh [options] [hostname...]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── helpers ───────────────────────────────────────────────────────────────────

die() { echo "ERROR: $*" >&2; exit 1; }

log_host() {
    local host="$1" step="$2" status="$3"
    printf '[%s] %s... %s\n' "$host" "$step" "$status"
}

run_cmd() {
    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "[DRY-RUN] $*"
        return 0
    fi
    if [[ "$VERBOSE" -eq 1 ]]; then
        "$@"
    else
        "$@" >/dev/null 2>&1
    fi
}

# ── dependency checks ─────────────────────────────────────────────────────────

command -v ssh   >/dev/null 2>&1 || die "ssh is required but not found in PATH"
command -v scp   >/dev/null 2>&1 || die "scp is required but not found in PATH"
command -v rsync >/dev/null 2>&1 || die "rsync is required but not found in PATH"

# ── SSH multiplexing options ──────────────────────────────────────────────────

SSH_MUX_OPTS=(
    -o ControlMaster=auto
    -o "ControlPath=/tmp/rclip-deploy-%r@%h:%p"
    -o ControlPersist=60
    -o ConnectTimeout=5
)

# ── defaults ──────────────────────────────────────────────────────────────────

INVENTORY="$REPO_DIR/deploy/hosts"
DRY_RUN=0
SKIP_INSTALL=0
SKIP_CONFIG=0
SKIP_RESTART=0
SKIP_SCRIPTS=0
ONLY_RESTART=0
PARALLEL=0
VERBOSE=0
declare -a TARGET_HOSTS=()

# ── usage ─────────────────────────────────────────────────────────────────────

usage() {
    cat <<EOF
Usage: $0 [options] [hostname...]

Deploy rclipboard to one or more remote hosts over SSH.
Without hostname arguments, deploys to all hosts in the inventory.

Options:
  --inventory FILE   Inventory file (default: deploy/hosts)
  --dry-run          Print commands without executing
  --skip-install     Skip pip reinstall
  --skip-config      Skip config.toml sync
  --skip-restart     Skip service restart
  --skip-scripts     Skip ~/bin script sync
  --only-restart     Restart only (implies --skip-install/config/scripts)
  --parallel         Deploy to all hosts in parallel
  --verbose          Show SSH/rsync output
  -h, --help         Show this help

Inventory format (deploy/hosts):
  hostname    init_system(systemd|none)    endpoint_type(uds|tcp)    [extra_ssh_opts]

Config templates:
  deploy/config/<hostname>.toml        host-specific override (takes precedence)
  deploy/config/config.<eptype>.toml   type-based template fallback
EOF
}

# ── option parsing ────────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case "$1" in
        --inventory)    INVENTORY="$2"; shift 2 ;;
        --dry-run)      DRY_RUN=1;      shift   ;;
        --skip-install) SKIP_INSTALL=1; shift   ;;
        --skip-config)  SKIP_CONFIG=1;  shift   ;;
        --skip-restart) SKIP_RESTART=1; shift   ;;
        --skip-scripts) SKIP_SCRIPTS=1; shift   ;;
        --only-restart) ONLY_RESTART=1; shift   ;;
        --parallel)     PARALLEL=1;     shift   ;;
        --verbose)      VERBOSE=1;      shift   ;;
        -h|--help)      usage; exit 0           ;;
        --)             shift; TARGET_HOSTS+=("$@"); break ;;
        -*)             die "Unknown option: $1" ;;
        *)              TARGET_HOSTS+=("$1"); shift ;;
    esac
done

if [[ "$ONLY_RESTART" -eq 1 ]]; then
    SKIP_INSTALL=1
    SKIP_CONFIG=1
    SKIP_SCRIPTS=1
fi

# ── inventory loading ─────────────────────────────────────────────────────────

declare -gA HOST_INIT_SYSTEM=()
declare -gA HOST_ENDPOINT_TYPE=()
declare -gA HOST_EXTRA_OPTS=()

load_inventory() {
    local file="$1"
    [[ -f "$file" ]] || die "Inventory file not found: $file"

    local _line _host _init _eptype _extra
    while IFS= read -r _line; do
        # strip leading whitespace
        _line="${_line#"${_line%%[! $'\t']*}"}"
        [[ -z "$_line" || "$_line" == \#* ]] && continue
        _extra=""
        read -r _host _init _eptype _extra <<< "$_line"
        [[ -z "$_host" ]] && continue

        case "$_init" in
            systemd|none) ;;
            *) echo "WARNING: unknown init_system '$_init' for '$_host' — skipping" >&2; continue ;;
        esac
        case "$_eptype" in
            uds|tcp) ;;
            *) echo "WARNING: unknown endpoint_type '$_eptype' for '$_host' — skipping" >&2; continue ;;
        esac

        HOST_INIT_SYSTEM["$_host"]="$_init"
        HOST_ENDPOINT_TYPE["$_host"]="$_eptype"
        HOST_EXTRA_OPTS["$_host"]="${_extra:-}"
    done < "$file"
}

# ── config template resolution ────────────────────────────────────────────────

resolve_config_template() {
    local host="$1" eptype="$2"
    local host_specific="$REPO_DIR/deploy/config/${host}.toml"
    local type_template="$REPO_DIR/deploy/config/config.${eptype}.toml"
    if [[ -f "$host_specific" ]]; then
        echo "$host_specific"
    elif [[ -f "$type_template" ]]; then
        echo "$type_template"
    else
        return 1
    fi
}

# ── per-host deploy ───────────────────────────────────────────────────────────

deploy_host() {
    local HOST="$1"
    local INIT_SYSTEM="$2"
    local EPTYPE="$3"
    local EXTRA_STR="${4:-}"

    declare -a SSH_EXTRA_OPTS=()
    if [[ -n "$EXTRA_STR" ]]; then
        eval "SSH_EXTRA_OPTS=($EXTRA_STR)"
    fi

    local -a SOPTS=("${SSH_MUX_OPTS[@]}" "${SSH_EXTRA_OPTS[@]+"${SSH_EXTRA_OPTS[@]}"}")

    # ── Step 1: SSH reachability ──────────────────────────────────────────────
    log_host "$HOST" "checking SSH" "..."
    if ! run_cmd ssh "${SOPTS[@]}" "$HOST" exit; then
        log_host "$HOST" "checking SSH" "FAILED (unreachable) — skipped"
        return 1
    fi
    log_host "$HOST" "checking SSH" "ok"

    # ── Step 2: Sync scripts ──────────────────────────────────────────────────
    if [[ "$SKIP_SCRIPTS" -eq 0 ]]; then
        log_host "$HOST" "syncing scripts" "..."
        run_cmd ssh "${SOPTS[@]}" "$HOST" \
            'mkdir -p ~/bin ~/.config/rclipboard/bin'
        run_cmd scp "${SOPTS[@]}" \
            "$REPO_DIR/scripts/bin/rclipctl" \
            "$HOST:~/bin/rclipctl"
        run_cmd scp "${SOPTS[@]}" \
            "$REPO_DIR/scripts/bin/rcliptunel" \
            "$HOST:~/bin/rcliptunel"
        run_cmd scp "${SOPTS[@]}" \
            "$REPO_DIR/scripts/bin/rclipboard-setup" \
            "$HOST:~/.config/rclipboard/bin/rclipboard-setup"
        log_host "$HOST" "syncing scripts" "ok"
    fi

    # ── Step 3: Sync package ──────────────────────────────────────────────────
    if [[ "$SKIP_INSTALL" -eq 0 ]]; then
        log_host "$HOST" "syncing package (rsync + pip)" "..."
        run_cmd rsync -az \
            --exclude='.venv' \
            --exclude='__pycache__' \
            --exclude='*.pyc' \
            --exclude='.git' \
            --exclude='deploy' \
            -e "ssh ${SOPTS[*]}" \
            "$REPO_DIR/" \
            "$HOST:/tmp/rclipboard-deploy/"
        run_cmd ssh "${SOPTS[@]}" "$HOST" \
            '~/.config/rclipboard/venv/bin/pip install /tmp/rclipboard-deploy/ --quiet'
        run_cmd ssh "${SOPTS[@]}" "$HOST" \
            'rm -rf /tmp/rclipboard-deploy/'
        log_host "$HOST" "syncing package (rsync + pip)" "ok"
    fi

    # ── Step 4: Sync config ───────────────────────────────────────────────────
    if [[ "$SKIP_CONFIG" -eq 0 ]]; then
        local tmpl tmpl_label
        if ! tmpl="$(resolve_config_template "$HOST" "$EPTYPE")"; then
            log_host "$HOST" "syncing config" "FAILED (no template for '$EPTYPE')"
            return 1
        fi
        if [[ "$tmpl" == *"/${HOST}.toml" ]]; then
            tmpl_label="host-specific override"
        else
            tmpl_label="${EPTYPE} template"
        fi
        log_host "$HOST" "syncing config (${tmpl_label})" "..."
        run_cmd ssh "${SOPTS[@]}" "$HOST" \
            'mkdir -p ~/.config/rclipboard'
        run_cmd scp "${SOPTS[@]}" \
            "$tmpl" \
            "$HOST:~/.config/rclipboard/config.toml"
        log_host "$HOST" "syncing config (${tmpl_label})" "ok"
    fi

    # ── Step 5: Restart ───────────────────────────────────────────────────────
    if [[ "$SKIP_RESTART" -eq 0 ]]; then
        log_host "$HOST" "restarting (${INIT_SYSTEM})" "..."
        if [[ "$INIT_SYSTEM" == "systemd" ]]; then
            run_cmd ssh "${SOPTS[@]}" "$HOST" \
                'systemctl --user restart rclipboard.service'
        else
            run_cmd ssh "${SOPTS[@]}" "$HOST" \
                'pkill -x rclipboard || true; sleep 1; nohup ~/.config/rclipboard/venv/bin/rclipboard >/tmp/rclipboard.log 2>&1 &'
        fi
        log_host "$HOST" "restarting (${INIT_SYSTEM})" "ok"
    fi

    return 0
}

# ── main ──────────────────────────────────────────────────────────────────────

load_inventory "$INVENTORY"

declare -a ALL_HOSTS=()

if [[ "${#TARGET_HOSTS[@]}" -gt 0 ]]; then
    for h in "${TARGET_HOSTS[@]}"; do
        [[ -v HOST_INIT_SYSTEM["$h"] ]] || die "Host '$h' not found in $INVENTORY"
        ALL_HOSTS+=("$h")
    done
else
    ALL_HOSTS=("${!HOST_INIT_SYSTEM[@]}")
fi

[[ "${#ALL_HOSTS[@]}" -gt 0 ]] || die "No hosts to deploy to (inventory empty or no hosts matched)"

declare -a FAILED_HOSTS=()

if [[ "$PARALLEL" -eq 0 ]]; then
    for HOST in "${ALL_HOSTS[@]}"; do
        if deploy_host \
                "$HOST" \
                "${HOST_INIT_SYSTEM[$HOST]}" \
                "${HOST_ENDPOINT_TYPE[$HOST]}" \
                "${HOST_EXTRA_OPTS[$HOST]:-}"; then
            :
        else
            FAILED_HOSTS+=("$HOST")
        fi
    done
else
    declare -a PIDS=()
    for HOST in "${ALL_HOSTS[@]}"; do
        (
            deploy_host \
                "$HOST" \
                "${HOST_INIT_SYSTEM[$HOST]}" \
                "${HOST_ENDPOINT_TYPE[$HOST]}" \
                "${HOST_EXTRA_OPTS[$HOST]:-}"
        ) &
        PIDS+=("$!:$HOST")
    done
    for pid_host in "${PIDS[@]}"; do
        local_pid="${pid_host%%:*}"
        local_host="${pid_host#*:}"
        if ! wait "$local_pid"; then
            FAILED_HOSTS+=("$local_host")
        fi
    done
fi

echo
echo "─── Deploy Summary ──────────────────────────────────────────────────────"
printf '  Attempted: %d host(s)\n' "${#ALL_HOSTS[@]}"
if [[ "${#FAILED_HOSTS[@]}" -eq 0 ]]; then
    echo "  Result:    ALL OK"
    exit 0
else
    printf '  Failed:    %s\n' "${FAILED_HOSTS[*]}"
    exit 1
fi
