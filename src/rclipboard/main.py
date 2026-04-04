# from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from logging import Logger

from fastapi import FastAPI
from starlette.exceptions import HTTPException as StarletteHTTPException

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
    app.state.runtime_state_hooks = []
    # HTTP routes
    http_mod.install_module(app)
    # WS routes
    await ws_mod.install_module(app)

    if os.environ.get("RCLIPBOARD_FIFO", "0") != "0":
        fifo_mod.install_fifo(app)
    # optional xsel poller
    if os.environ.get("RCLIPBOARD_XSEL", "0") != "0":
        xsel_mod.install_xsel(app)
    # optional proxy
    proxy_mod.install_proxy(app)


async def shutdown(app: FastAPI):
    xsel_enabled = os.environ.get("RCLIPBOARD_XSEL", "0") != "0"
    fifo_enabled = os.environ.get("RCLIPBOARD_FIFO", "0") != "0"
    async with asyncio.TaskGroup() as tg:
        tg.create_task(proxy_mod.shutdown_proxy(app))
        if fifo_enabled:
            tg.create_task(fifo_mod.shutdown_fifo(app))
        if xsel_enabled:
            tg.create_task(xsel_mod.shutdown_xsel(app))

    main: AppState = app.state.main
    await main.cancel_background_tasks()
    await main.flush_all_notifications()
    task = main.dispatcher_task
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
    app.add_exception_handler(StarletteHTTPException,
                              http_mod._http_exception_handler)
    return app


app = create_app()
