FROM ubuntu:26.04

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

# Runtime + build dependencies:
#   python3 / python3-venv  — interpreter and venv support (rclipboard needs >=3.11)
#   ca-certificates, curl    — TLS roots; curl is also used by the rclipctl client
#   jq                       — required by the rclipctl client
#   age                      — client-side age encryption (rclipctl exec / --encrypt)
RUN apt-get update -y && apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
        ca-certificates \
        curl \
        jq \
        age \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged runtime user that owns the app and its virtualenv.
# /app is created and chowned to `user` so the venv can be built unprivileged.
RUN useradd --create-home --shell /usr/sbin/nologin user \
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

# rclipctl and friends; PYTHONPATH lets `rclipboard.main:app` import from src.
ENV PATH=/app/scripts/bin:$PATH \
    PYTHONPATH=/app/src \
    RCLIPBOARD_BIND_ADDR=0.0.0.0 \
    RCLIPBOARD_BIND_PORT=8989 \
    RCLIPBOARD_LOG_LEVEL=info \
    RCLIPBOARD_PY_LOG_LEVEL=INFO

EXPOSE 8989

CMD ["python", "-m", "uvicorn", "rclipboard.main:app", \
     "--host", "0.0.0.0", "--port", "8989", "--log-level", "info"]
