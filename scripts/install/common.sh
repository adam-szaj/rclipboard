#!/usr/bin/env bash

PUBLIC_SCRIPTS='rclipctl
rcliptunel
rclipboard-launcher
rclipboard-service-run
rclipboard-setup
rclipboard-update
rclipboard-uninstall'

INSTALLER_FILES='install.sh
install/common.sh
install/systemd.sh
install/launchd.sh
systemd/user/rclipboard.service
systemd/user/rclipboard-display.service
launchd/com.rclipboard.service.plist.in
config/rclipboard.conf.example'

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

warn() {
    printf 'warning: %s\n' "$*" >&2
}

resolve_app_dir() {
    printf '%s/rclipboard\n' "${XDG_CONFIG_HOME:-$HOME/.config}"
}

require_python_311() {
    "$PYTHON_BIN" -c \
        'import sys; raise SystemExit(sys.version_info < (3, 11))' \
        || die "Python 3.11 or newer is required"
}

require_git_checkout() {
    local inside bare

    inside=$(git -C "$REPO_DIR" rev-parse --is-inside-work-tree 2>/dev/null) \
        || die "source directory is not a Git checkout: $REPO_DIR"
    [ "$inside" = true ] \
        || die "source directory is not a Git checkout: $REPO_DIR"
    bare=$(git -C "$REPO_DIR" rev-parse --is-bare-repository 2>/dev/null) \
        || die "cannot inspect Git checkout: $REPO_DIR"
    [ "$bare" = false ] || die "bare Git repositories are not supported"
}

current_git_branch() {
    git -C "$REPO_DIR" symbolic-ref --quiet --short HEAD \
        || die "installation from detached HEAD is not supported"
}

require_git_target() {
    git -C "$REPO_DIR" remote get-url "$REMOTE" >/dev/null 2>&1 \
        || die "Git remote does not exist: $REMOTE"
    git check-ref-format --branch "$BRANCH" >/dev/null 2>&1 \
        || die "invalid Git branch: $BRANCH"
    if ! git -C "$REPO_DIR" show-ref --verify --quiet \
        "refs/heads/$BRANCH" \
        && ! git -C "$REPO_DIR" show-ref --verify --quiet \
            "refs/remotes/$REMOTE/$BRANCH"; then
        die "Git branch does not exist locally or for $REMOTE: $BRANCH"
    fi
}

require_single_line_value() {
    local name="$1" value="$2" newline
    newline='
'

    [ -n "$value" ] || die "$name must not be empty"
    case "$value" in
        *"$newline"*) die "$name must be a single-line value" ;;
    esac
}

read_install_metadata() {
    local path="$1" key value
    local metadata_repo="" metadata_remote="" metadata_branch=""
    local seen_repo=0 seen_remote=0 seen_branch=0

    [ -f "$path" ] && [ ! -L "$path" ] \
        || die "installation metadata is missing or unsafe: $path"
    while IFS='=' read -r key value || [ -n "$key$value" ]; do
        case "$key" in
            repo_dir)
                [ "$seen_repo" -eq 0 ] \
                    || die "duplicate metadata key: repo_dir"
                require_single_line_value repo_dir "$value"
                metadata_repo="$value"
                seen_repo=1
                ;;
            remote)
                [ "$seen_remote" -eq 0 ] \
                    || die "duplicate metadata key: remote"
                require_single_line_value remote "$value"
                metadata_remote="$value"
                seen_remote=1
                ;;
            branch)
                [ "$seen_branch" -eq 0 ] \
                    || die "duplicate metadata key: branch"
                require_single_line_value branch "$value"
                metadata_branch="$value"
                seen_branch=1
                ;;
            *) die "unknown metadata key: $key" ;;
        esac
    done < "$path"
    [ "$seen_repo" -eq 1 ] || die "missing metadata key: repo_dir"
    [ "$seen_remote" -eq 1 ] || die "missing metadata key: remote"
    [ "$seen_branch" -eq 1 ] || die "missing metadata key: branch"

    REPO_DIR="$metadata_repo"
    REMOTE="$metadata_remote"
    BRANCH="$metadata_branch"
}

write_install_metadata() {
    local path="$1" repo="$2" remote="$3" branch="$4"
    local temporary="${path}.tmp.$$"

    require_single_line_value repo_dir "$repo"
    require_single_line_value remote "$remote"
    require_single_line_value branch "$branch"
    if ! (umask 077; printf 'repo_dir=%s\nremote=%s\nbranch=%s\n' \
        "$repo" "$remote" "$branch" > "$temporary"); then
        rm -f "$temporary"
        return 1
    fi
    chmod 0600 "$temporary" || {
        rm -f "$temporary"
        return 1
    }
    mv -f "$temporary" "$path" || {
        rm -f "$temporary"
        return 1
    }
}

require_install_sources() {
    local relative

    while IFS= read -r relative; do
        [ -f "$INSTALL_SOURCE_DIR/bin/$relative" ] \
            || die "missing public installer source: $relative"
    done <<EOF
$PUBLIC_SCRIPTS
EOF
    while IFS= read -r relative; do
        [ -f "$INSTALL_SOURCE_DIR/$relative" ] \
            || die "missing installer payload source: $relative"
    done <<EOF
$INSTALLER_FILES
EOF
}

install_public_scripts() {
    local relative

    while IFS= read -r relative; do
        install -m 0755 "$INSTALL_SOURCE_DIR/bin/$relative" \
            "$BIN_DIR/$relative" || return 1
    done <<EOF
$PUBLIC_SCRIPTS
EOF
}

install_installer_payload() {
    local relative target mode

    while IFS= read -r relative; do
        target="$INSTALLER_DIR/$relative"
        mkdir -p "$(dirname "$target")" || return 1
        case "$relative" in
            *.sh) mode=0755 ;;
            *) mode=0644 ;;
        esac
        install -m "$mode" "$INSTALL_SOURCE_DIR/$relative" "$target" \
            || return 1
    done <<EOF
$INSTALLER_FILES
EOF
}

managed_file_has_marker() {
    local path="$1" marker="$2" format="${3:-plain}"
    local first_line="" second_line=""

    [ -f "$path" ] && [ ! -L "$path" ] || return 1
    {
        IFS= read -r first_line || true
        IFS= read -r second_line || true
    } < "$path"
    if [ "$format" = xml ]; then
        [ "$first_line" = '<?xml version="1.0" encoding="UTF-8"?>' ] \
            && [ "$second_line" = "$marker" ]
    else
        [ "$first_line" = "$marker" ]
    fi
}

check_managed_file_collision() {
    local path="$1" marker="$2" format="${3:-plain}"

    if [ -e "$path" ] || [ -L "$path" ]; then
        if ! managed_file_has_marker "$path" "$marker" "$format"; then
            printf 'error: refusing to overwrite unmanaged file: %s\n' "$path" >&2
            return 1
        fi
    fi
}

remove_marked_file() {
    local path="$1" marker="$2" format="${3:-plain}"

    if managed_file_has_marker "$path" "$marker" "$format"; then
        rm -f "$path"
    fi
}

render_template() {
    local source="$1" target="$2" placeholder="$3" value="$4"
    local escaped_value temporary

    escaped_value=$(printf '%s' "$value" | sed 's/[\\&|]/\\&/g') || return 1
    temporary="${target}.tmp.$$"
    if ! sed "s|$placeholder|$escaped_value|g" "$source" > "$temporary"; then
        rm -f "$temporary"
        return 1
    fi
    chmod 0644 "$temporary" || {
        rm -f "$temporary"
        return 1
    }
    mv -f "$temporary" "$target" || {
        rm -f "$temporary"
        return 1
    }
}

xml_escape() {
    printf '%s' "$1" | sed \
        -e 's/&/\&amp;/g' \
        -e 's/</\&lt;/g' \
        -e 's/>/\&gt;/g' \
        -e 's/"/\&quot;/g' \
        -e "s/'/\&apos;/g"
}

render_xml_template() {
    local source="$1" target="$2" placeholder="$3" value="$4"
    local escaped_value

    escaped_value=$(xml_escape "$value") || return 1
    render_template "$source" "$target" "$placeholder" "$escaped_value"
}
