FROM ubuntu:26.04

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# This is an INTERACTIVE TEST image: ssh in as `user`, then run the server /
# rclipctl by hand. sshd is PID 1; the rclipboard server is NOT started for you.
#
# Runtime + build dependencies:
#   python3 / python3-venv   — interpreter and venv support (rclipboard needs >=3.11)
#   ca-certificates, curl     — TLS roots; curl is also used by the rclipctl client
#   jq                        — required by the rclipctl client
#   age                       — client-side age encryption (rclipctl exec / --encrypt)
#   openssh-server / -client  — sshd for interactive login + ssh for outbound/tunnels
#   bash                      — interactive login shell
# DEBIAN_FRONTEND is scoped to this RUN only (not a persistent ENV) so it does
# not affect `apt` run interactively over SSH later.
RUN DEBIAN_FRONTEND=noninteractive apt-get update -y \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
        ca-certificates \
        curl \
        jq \
        age \
        openssh-server \
        openssh-client \
        bash \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/sshd \
    && ssh-keygen -A

# Unprivileged runtime user that owns the app and its virtualenv, and is the
# account you log into over SSH (bash login shell).
# /app is created and chowned to `user` so the venv can be built unprivileged.
RUN useradd --create-home --shell /bin/bash user \
    && mkdir -p /app \
    && chown user:user /app

WORKDIR /app

# uv lives in the venv; the venv is owned by `user` so the install (and any
# later `uv pip` calls) run unprivileged and avoid the system Python, which is
# externally managed (PEP 668).
ENV VENV=/app/.venv \
    PATH=/app/.venv/bin:$PATH

COPY --chown=user:user pyproject.toml README.md ./
COPY --chown=user:user src ./src
COPY --chown=user:user scripts ./scripts

USER user

RUN python3 -m venv "$VENV" \
    && "$VENV/bin/pip" install --no-cache-dir uv \
    && "$VENV/bin/uv" pip install -r pyproject.toml \
    && "$VENV/bin/uv" pip install -e .

# Generate an ed25519 key pair for `user` and authorize it for SSH login. The
# private key (/home/user/.ssh/id_ed25519) is baked into the image so you can
# `ssh -i` immediately — fine for an interactive test image, NOT for production.
RUN install -d -m 700 /home/user/.ssh \
    && ssh-keygen -t ed25519 -N '' -C 'rclipboard-test' -f /home/user/.ssh/id_ed25519 \
    && cp /home/user/.ssh/id_ed25519.pub /home/user/.ssh/authorized_keys \
    && chmod 600 /home/user/.ssh/id_ed25519 /home/user/.ssh/authorized_keys

# sshd config: key-only login for `user`, no root, no passwords.
USER root
RUN printf '%s\n' \
    'PermitRootLogin no' \
    'PasswordAuthentication no' \
    'KbdInteractiveAuthentication no' \
    'PubkeyAuthentication yes' \
    'AllowUsers user' \
    'AllowTcpForwarding yes' \
    'AllowStreamLocalForwarding yes' \
    'StreamLocalBindUnlink yes' \
    > /etc/ssh/sshd_config.d/rclipboard-test.conf

# rclipctl and friends; PYTHONPATH lets `rclipboard.main:app` import from src.
# Set on the global environment so interactive SSH sessions (run as `user`)
# inherit them via /etc/environment.
ENV PATH=/app/.venv/bin:/app/scripts/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONPATH=/app/src \
    RCLIPBOARD_BIND_ADDR=0.0.0.0 \
    RCLIPBOARD_BIND_PORT=8989 \
    RCLIPBOARD_LOG_LEVEL=info \
    RCLIPBOARD_PY_LOG_LEVEL=INFO
RUN printf '%s\n' \
    'PATH=/app/.venv/bin:/app/scripts/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' \
    'PYTHONPATH=/app/src' \
    > /etc/environment

# 22 = sshd (interactive login), 8989 = rclipboard server (started by hand)
EXPOSE 22 8989

# sshd in the foreground as PID 1; ssh in as `user` to drive the tests.
CMD ["/usr/sbin/sshd", "-D", "-e"]
