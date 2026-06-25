FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update -y && apt-get install -y --no-install-recommends \
    ca-certificates curl jq && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts

RUN pip install --no-cache-dir uv && \
    uv pip install --system -r pyproject.toml && \
    uv pip install --system -e .

ENV RCLIPBOARD_BIND_ADDR=0.0.0.0 \
    RCLIPBOARD_BIND_PORT=8989 \
    RCLIPBOARD_LOG_LEVEL=info \
    RCLIPBOARD_PY_LOG_LEVEL=INFO \
    PYTHONPATH=/app/src

EXPOSE 8989

CMD ["python", "-m", "uvicorn", "rclipboard.main:app", \
     "--host", "0.0.0.0", "--port", "8989", "--log-level", "info"]
