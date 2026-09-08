#!/usr/bin/env bash

managed_file_has_marker() {
    local path="$1" marker="$2" first_line="" second_line=""

    [ -f "$path" ] && [ ! -L "$path" ] || return 1
    {
        IFS= read -r first_line || true
        IFS= read -r second_line || true
    } < "$path"
    [ "$first_line" = "$marker" ] || {
        [ "$first_line" = '<?xml version="1.0" encoding="UTF-8"?>' ] \
            && [ "$second_line" = "$marker" ]
    }
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
