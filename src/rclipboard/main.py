# from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from logging import Logger

from fastapi import FastAPI

import rclipboard.fifo as fifo_mod
import rclipboard.http as http_mod
import rclipboard.proxy as proxy_mod
import rclipboard.ws as ws_mod
import rclipboard.xsel as xsel_mod
from rclipboard.app_state import AppState
from rclipboard.log import get_logger

logger: Logger = get_logger(__name__)
error = logger.error
warning = logger.warning
info = logger.info
debug = logger.debug
trace = logger.debug


async def startup(app: FastAPI):
    app.state.main = AppState(app)
    app.state.local_topic_data_hooks = []
    app.state.runtime_state_hooks = []
    # HTTP routes
    http_mod.install_module(app)
    # WS routes
    await ws_mod.install_module(app)

    fifo_mod.install_fifo(app)
    # optional xsel poller
    if os.environ.get("RCLIPBOARD_XSEL", "0") != "0":
        xsel_mod.install_xsel(app)
    # optional proxy
    proxy_mod.install_proxy(app)


async def shutdown(app: FastAPI):
    await proxy_mod.shutdown_proxy(app)

    if os.environ.get("RCLIPBOARD_XSEL", "0") != "0":
        await xsel_mod.shutdown_xsel(app)
    await fifo_mod.shutdown_fifo(app)

    task = getattr(app.state.main, "dispatcher_task", None)
    await app.state.main.flush_all_notifications()
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    await startup(app)
    yield
    await shutdown(app)


def create_app() -> FastAPI:
    app = FastAPI(title="rclipboard", version="0.1.0", lifespan=lifespan)
    return app


app = create_app()
