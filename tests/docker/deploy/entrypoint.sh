#!/bin/sh
set -e
mkdir -p /root/.ssh
printf '%s\n' "${AUTHORIZED_KEY}" > /root/.ssh/authorized_keys
chmod 700 /root/.ssh
chmod 600 /root/.ssh/authorized_keys
# -D: foreground  -e: log to stderr
exec /usr/sbin/sshd -D -e
