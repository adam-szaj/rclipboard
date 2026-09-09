FROM debian:12-slim

RUN apt-get update -y && \
    apt-get install -y --no-install-recommends \
        openssh-server \
        python3 \
        python3-venv \
        python3-pip \
        procps \
        git \
        rsync && \
    rm -rf /var/lib/apt/lists/* && \
    mkdir -p /run/sshd /root/.ssh && \
    ssh-keygen -A

# Key-only root login; all forwarding types enabled; auto-unlink stale sockets.
RUN printf '%s\n' \
    'PermitRootLogin yes' \
    'PasswordAuthentication no' \
    'ChallengeResponseAuthentication no' \
    'GatewayPorts clientspecified' \
    'AllowTcpForwarding yes' \
    'AllowStreamLocalForwarding yes' \
    'StreamLocalBindUnlink yes' \
    >> /etc/ssh/sshd_config

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 22

CMD ["/usr/local/bin/entrypoint.sh"]
