#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_DIR="${1:-$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)}"
printf '%s\n' \
    'warning: install-systemd-user.sh is deprecated; use scripts/install.sh' >&2
exec "$REPO_DIR/scripts/install.sh"
