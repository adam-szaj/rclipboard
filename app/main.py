from __future__ import annotations

import asyncio
import os
from typing import Any

from fastapi import FastAPI

from . import http as http_mod
from . import ws as ws_mod
from . import xsel as xsel_mod
from . import proxy as proxy_mod


def create_app() -> FastAPI:
    app = FastAPI(title="rclipboard", version="0.1.0")

    # Shared state
    app.state.bus = asyncio.Queue()
    app.state.topic_content: dict[str, dict] = {}
    app.state.subs: dict[str, set[ws_mod.Connection]] = {}

    @app.on_event("startup")
    async def _startup():
        app.state.dispatcher_task = asyncio.create_task(ws_mod.dispatcher(app),
                                                        name="dispatcher")
        # optional xsel poller
        xsel_mod.install_xsel(app)
        # optional proxy
        proxy_mod.install_proxy(app)

    @app.on_event("shutdown")
    async def _shutdown():
        task = getattr(app.state, "dispatcher_task", None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # HTTP routes
    http_mod.install_http_handlers(app)

    # WS route
    ws_mod.install_ws(app)

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
    server = uvicorn.Server(config)
    server.run()
