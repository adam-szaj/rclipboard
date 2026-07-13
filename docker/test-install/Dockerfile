# Installer tests WITHOUT systemd: clean environment, fake systemctl stub
# (provided by the test suite itself). Verifies layout, units content,
# config generation and idempotency.
#
# Build & run:  make test-install-docker
FROM ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash python3 python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash tester
USER tester
WORKDIR /home/tester/rclipboard

COPY --chown=tester:tester . .

# The suite runs `rclipboard config env` via sys.executable — the package
# and its deps must be importable by the interpreter running the tests.
RUN python3 -m venv /home/tester/venv \
    && /home/tester/venv/bin/pip install --quiet .

ENV PYTHONPATH=/home/tester/rclipboard/src:/home/tester/rclipboard
CMD ["/home/tester/venv/bin/python", "-m", "unittest", "tests.test_installer", "-v"]
