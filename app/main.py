from __future__ import annotations
from contextlib import asynccontextmanager
import asyncio
import os
from sys import argv
from typing import Any

from fastapi import FastAPI

from . import http as http_mod
from . import ws as ws_mod
from . import xsel as xsel_mod
from . import proxy as proxy_mod


from .log import get_logger

def error(*args):
    get_logger(__name__).error(*args)

def warning(*args):
    get_logger(__name__).warning(*args)

def info(*args):
    get_logger(__name__).info(*args)

def debug(*args):
    get_logger(__name__).debug(*args)

def trace(*args):
    get_logger(__name__).trace(*args)


def print_env():
    uds = os.environ.get("RCLIPBOARD_BIND_UDS")
    host = os.environ.get("RCLIPBOARD_BIND_ADDR", "127.0.0.1")
    port = int(os.environ.get("RCLIPBOARD_BIND_PORT", 8989))
    proxy = os.environ.get("RCLIPBOARD_PROXY")
    proxy_uds = os.environ.get("RCLIPBOARD_PROXY_UDS")
    proxy_host = os.environ.get("RCLIPBOARD_PROXY_ADDR", "127.0.0.1")
    proxy_port = int(os.environ.get("RCLIPBOARD_PROXY_PORT", 8989))

    warning(f"args: {argv}\n"
            f"uds                     {uds}\n"
            f"host                    {host}\n"
            f"port                    {port}\n"
            f"proxy                   {proxy}\n"
            f"proxy_uds               {proxy_uds}\n"
            f"proxy_host              {proxy_host}\n"
            f"proxy_port              {proxy_port}\n")


class Bus:
    def __init__(self):
        self.queue = asyncio.Queue()

    async def qput(self, data):
        return await self.queue.put(data)

    async def qget(self):
        return await self.queue.get()


async def qput(app: FastAPI, data):
    return await app.state._bus.qput(data)


async def qget(app: FastAPI):
    return await app.state._bus.qget()


async def _startup(app: FastAPI):

    app.state._bus = Bus()
    app.state.topic_content = dict()
    app.state.subs = dict()

    http_mod.install_http_handlers(app)
    # WS route
    ws_mod.install_ws(app)

    app.state.dispatcher_task = asyncio.create_task(ws_mod.dispatcher(app),
                                                    name="dispatcher")
    # HTTP routes
    # optional xsel poller
    xsel_mod.install_xsel(app)
    # optional proxy
    proxy_mod.install_proxy(app)


async def _shutdown(app: FastAPI):
    task = getattr(app.state, "dispatcher_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _startup(app)
    yield
    await _shutdown(app)


def create_app() -> FastAPI:
    print_env()
    app = FastAPI(title="rclipboard", version="0.1.0", lifespan=lifespan)


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

    warning(f"uds                     {uds}\n"
            f"host                    {host}\n"
            f"port                    {port}\n"
            f"ssl_certfile            {ssl_certfile}\n"
            f"ssl_keyfile             {ssl_keyfile}\n"
            f"ssl_keyfile_password    {ssl_keyfile_password}\n")
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
