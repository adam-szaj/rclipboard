from __future__ import annotations

import asyncio as a
import contextlib
import json
import os
from pathlib import Path
from typing import override

from fastapi import FastAPI
from pydantic import JsonValue

from rclipboard.core.state import register_client
from rclipboard.log import get_logger as gl
from rclipboard.models.rpc import notification_payload, response_payload
from rclipboard.models.wire import RPCError
from rclipboard.transports.rpc_handler import RPCHandler

logger = gl(__name__)
error = logger.error
warning = logger.warning
info = logger.info
debug = logger.debug
trace = logger.debug


class UDSServerConnection(RPCHandler):

    # Monitoring classification (see Interface.monitor_kind).
    monitor_kind: str = "uds"

    def __init__(self, app: FastAPI, reader: a.StreamReader,
                 writer: a.StreamWriter):
        RPCHandler.__init__(self, app)
        self.reader = reader
        self.writer = writer

    @property
    @override
    def name(self) -> str:
        return f"udsconn:{id(self):x}"

    async def _write_line(self, payload: str) -> None:
        self.writer.write((payload + "\n").encode("utf-8"))
        await self.writer.drain()

    async def _send_result(self, request_id: int | str | None,
                           result: JsonValue) -> None:
        if request_id is None:
            return
        await self._write_line(
            json.dumps(response_payload(request_id, result=result)))

    async def _send_error(self, request_id: int | str | None,
                          rpc_error: RPCError) -> None:
        if request_id is None:
            return
        await self._write_line(
            json.dumps(response_payload(request_id, error=rpc_error)))

    async def _send_event(self, method: str, params: object) -> None:
        await self._write_line(json.dumps(notification_payload(method, params)))

    async def loop(self) -> None:
        register_client(self.app, self)
        try:
            while True:
                try:
                    raw_line = await self.reader.readline()
                except (ConnectionResetError, BrokenPipeError, OSError,
                        a.LimitOverrunError):
                    break
                if not raw_line:  # EOF — clean disconnect
                    break
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    json_message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                await self._dispatch_message(json_message)
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            await self._teardown_connection()
            with contextlib.suppress(Exception):
                self.writer.close()
                await self.writer.wait_closed()


class RawUDSServer:

    def __init__(self, app: FastAPI, socket_path: str):
        self.app = app
        self.socket_path = socket_path
        self._server: a.AbstractServer | None = None
        self._connection_tasks: set[a.Task] = set()

    async def start(self) -> None:
        with contextlib.suppress(OSError):
            Path(self.socket_path).unlink()
        self._server = await a.start_unix_server(
            self._handle_connection,
            path=self.socket_path,
            limit=4 * 1024 * 1024,  # 4 MiB: covers large base64 clipboard payloads
        )
        Path(self.socket_path).chmod(0o600)
        info(f"raw UDS server listening on {self.socket_path}")

    async def _handle_connection(self, reader: a.StreamReader,
                                 writer: a.StreamWriter) -> None:
        conn = UDSServerConnection(self.app, reader, writer)
        task = a.current_task()  # asyncio creates a task per connection
        if task:
            self._connection_tasks.add(task)
        try:
            await conn.loop()
        finally:
            if task:
                self._connection_tasks.discard(task)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for task in list(self._connection_tasks):
            task.cancel()
        for task in list(self._connection_tasks):
            with contextlib.suppress(a.CancelledError):
                await task
        self._connection_tasks.clear()
        with contextlib.suppress(OSError):
            Path(self.socket_path).unlink()
        info(f"raw UDS server stopped, socket removed: {self.socket_path}")


def install_raw_uds(app: FastAPI) -> None:
    path = os.environ.get("RCLIPBOARD_RAW_UDS_PATH", "").strip()
    app.state.raw_uds_enabled = bool(path)
    app.state.raw_uds_server = None
    app.state.raw_uds_start_task = None
    if not path:
        return
    server = RawUDSServer(app, path)
    app.state.raw_uds_server = server
    app.state.raw_uds_start_task = a.create_task(server.start(),
                                                  name="raw_uds_start")


async def shutdown_raw_uds(app: FastAPI) -> None:
    start_task = getattr(app.state, "raw_uds_start_task", None)
    if start_task and not start_task.done():
        start_task.cancel()
        with contextlib.suppress(a.CancelledError):
            await start_task
    server: RawUDSServer | None = getattr(app.state, "raw_uds_server", None)
    if server:
        await server.stop()
    app.state.raw_uds_server = None
    app.state.raw_uds_start_task = None
