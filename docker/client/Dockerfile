FROM debian:12-slim

RUN apt-get update -y && apt-get install -y --no-install-recommends \
    curl jq wget tar gzip bash && \
    rm -rf /var/lib/apt/lists/*

# Install age (encryption tool)
ARG AGE_VERSION=1.2.1
RUN arch="$(dpkg --print-architecture)" && \
    case "$arch" in \
        amd64)  age_arch="linux-amd64" ;; \
        arm64)  age_arch="linux-arm64" ;; \
        *)      echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac && \
    wget -qO /tmp/age.tar.gz \
        "https://github.com/FiloSottile/age/releases/download/v${AGE_VERSION}/age-v${AGE_VERSION}-${age_arch}.tar.gz" && \
    tar -xz -C /tmp -f /tmp/age.tar.gz && \
    install -m 0755 /tmp/age/age /usr/local/bin/age && \
    install -m 0755 /tmp/age/age-keygen /usr/local/bin/age-keygen && \
    rm -rf /tmp/age /tmp/age.tar.gz

COPY scripts/bin/rclipctl  /usr/local/bin/rclipctl
COPY scripts/bin/rcliptunel /usr/local/bin/rcliptunel
RUN chmod +x /usr/local/bin/rclipctl /usr/local/bin/rcliptunel

ENV RCLIPBOARD_ADMIN_TOKEN=devtoken

CMD ["bash"]
