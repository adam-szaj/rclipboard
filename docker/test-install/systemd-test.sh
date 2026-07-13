#!/usr/bin/env bash
# Runs inside the systemd container (docker exec, as root). Verifies the
# installer against a REAL `systemctl --user`:
#   1. layout tests (same suite as the non-systemd variant)
#   2. real install for user `tester` (pip install into the venv)
#   3. unit loadable + service starts and answers /v1/health.get
set -euo pipefail

REPO=/home/tester/rclipboard

echo "── 1/3 layout test suite ──"
su - tester -c "cd $REPO && PYTHONPATH=$REPO/src:$REPO /home/tester/venv/bin/python -m unittest tests.test_installer -v"

echo "── 2/3 real install (systemctl --user via lingering) ──"
loginctl enable-linger tester
# Wait for the per-user systemd instance (dbus user session).
for _ in $(seq 1 30); do
    [ -S "/run/user/$(id -u tester)/bus" ] && break
    sleep 0.5
done
[ -S "/run/user/$(id -u tester)/bus" ] || { echo "user bus never appeared" >&2; exit 1; }

_user() {
    local uid; uid=$(id -u tester)
    su - tester -c "XDG_RUNTIME_DIR=/run/user/$uid DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid/bus $*"
}

_user "bash $REPO/scripts/install-systemd-user.sh $REPO"

echo "── 3/3 unit loadable + service starts ──"
_user "systemctl --user cat rclipboard.service > /dev/null"
_user "systemctl --user start rclipboard.service"
# The default config.toml binds a UDS socket under $XDG_RUNTIME_DIR.
UDS_SOCK="/run/user/$(id -u tester)/rclipboard/uds.sock"
_health() {
    _user "curl -fsS --unix-socket $UDS_SOCK http://localhost/v1/health.get"
}
for _ in $(seq 1 20); do
    _health >/dev/null 2>&1 && break
    sleep 0.5
done
_health | grep -q '"ok"' || {
    echo "── service did not answer; diagnostics ──" >&2
    _user "systemctl --user status rclipboard.service --no-pager" >&2 || true
    _user "journalctl --user -u rclipboard.service -n 40 --no-pager" >&2 || true
    exit 1
}
_user "systemctl --user stop rclipboard.service"

echo "PASS: installer works under real systemd"
