#!/usr/bin/env bash

managed_file_has_marker() {
    local path="$1" marker="$2" first_line=""

    [ -f "$path" ] && [ ! -L "$path" ] || return 1
    IFS= read -r first_line < "$path" || true
    [ "$first_line" = "$marker" ]
}

check_managed_file_collision() {
    local path="$1" marker="$2"

    if [ -e "$path" ] || [ -L "$path" ]; then
        if ! managed_file_has_marker "$path" "$marker"; then
            printf 'error: refusing to overwrite unmanaged file: %s\n' "$path" >&2
            return 1
        fi
    fi
}

remove_marked_file() {
    local path="$1" marker="$2"

    if managed_file_has_marker "$path" "$marker"; then
        rm -f "$path"
    fi
}

render_template() {
    local source="$1" target="$2" placeholder="$3" value="$4"
    local escaped_value temporary

    escaped_value=$(printf '%s' "$value" | sed 's/[\\&|]/\\&/g')
    temporary="${target}.tmp.$$"
    if sed "s|$placeholder|$escaped_value|g" "$source" > "$temporary"; then
        chmod 0644 "$temporary"
        mv -f "$temporary" "$target"
    else
        rm -f "$temporary"
        return 1
    fi
}
