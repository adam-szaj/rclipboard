from __future__ import annotations
from contextlib import asynccontextmanager
from app.app_state import AppState
import asyncio
import os

from fastapi import FastAPI
from .app_state import AppState
from . import http as http_mod
from . import ws as ws_mod
from . import xsel as xsel_mod
from . import proxy as proxy_mod

from logging import Logger
from .log import get_logger

logger: Logger = get_logger(__name__)
error = logger.error
warning = logger.warning
info = logger.info
debug = logger.debug
trace = logger.debug


async def startup(app: FastAPI):
    app.state.main = AppState(app)
    # HTTP routes
    http_mod.install_http_handlers(app)

    # WS route
    ws_mod.install_ws(app)

    # optional xsel poller
    xsel_mod.install_xsel(app)
    # optional proxy
    # proxy_mod.install_proxy(app)


async def shutdown(app: FastAPI):
    task = getattr(app.state, "dispatcher_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    await startup(app)
    yield
    await shutdown(app)


def create_app() -> FastAPI:
    app = FastAPI(title="rclipboard", version="0.1.0", lifespan=lifespan)
    info(f"create_app: {app}")
    # Shared state

    return app


app = create_app()

if __name__ == "__main__":
    # Local runner supporting TCP, UDS and HTTPS via env vars.
    import uvicorn

    uds = os.environ.get("RCLIPBOARD_BIND_UDS")
    host = os.environ.get("RCLIPBOARD_BIND_ADDR", "127.0.0.1")
    port = int(os.environ.get("RCLIPBOARD_BIND_PORT", 8989))
    ssl_certfile = os.environ.get("RCLIPBOARD_SSL_CERTFILE")
    ssl_keyfile = os.environ.get("RCLIPBOARD_SSL_KEYFILE")
    ssl_keyfile_password = os.environ.get("RCLIPBOARD_SSL_KEYFILE_PASSWORD")

    config = uvicorn.Config(
        app="app.main:app",
        host=None if uds else host,
        port=None if uds else port,
        uds=uds,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
        ssl_keyfile_password=ssl_keyfile_password,
        log_level=os.environ.get("RCLIPBOARD_LOG_LEVEL", "info"),
        reload=False,
    )

    import pdb

    pdb.set_trace()
    server = uvicorn.Server(config)
    server.run()
