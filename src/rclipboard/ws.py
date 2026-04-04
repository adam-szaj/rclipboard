from __future__ import annotations

import json
from typing import override

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import JsonValue, ValidationError

from .app_state import register_client, unregister_client, unsubscribe_client
from .log import get_logger as gl
from .rpc_handler import RPCHandler, RPCMethodError
from .types import JSONRPCRequestMessage, JSONRPCResponseMessage, RPCError, TopicData

logger = gl(__name__)
error = logger.error
warning = logger.warning
info = logger.info
debug = logger.debug
trace = logger.debug


class WSServerConnection(RPCHandler):

    def __init__(self, app: FastAPI, ws: WebSocket):
        RPCHandler.__init__(self, app)
        self.ws: WebSocket = ws

    @property
    @override
    def name(self) -> str:
        return "wsconnection"

    async def _send_result(self, request_id: int | str | None,
                           result: JsonValue) -> None:
        if request_id is None:
            return
        await self.ws.send_json(
            JSONRPCResponseMessage(
                **{
                    "id": request_id
                },
                jsonrpc="2.0",
                result=result,
            ).model_dump(by_alias=True, mode="json", exclude_none=True))

    async def _send_error(self, request_id: int | str | None,
                          rpc_error: RPCError) -> None:
        if request_id is None:
            return
        await self.ws.send_json(
            JSONRPCResponseMessage(
                **{
                    "id": request_id
                },
                jsonrpc="2.0",
                error=rpc_error,
            ).model_dump(by_alias=True, mode="json", exclude_none=True))

    async def _send_event(self, method: str, params: object) -> None:
        await self.ws.send_json({
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        })

    async def loop(self):
        await self.ws.accept()
        register_client(self.app, self)
        try:
            while True:
                json_message = await self.ws.receive_json()
                if not isinstance(json_message, dict):
                    continue
                if "method" not in json_message:
                    continue
                try:
                    request = JSONRPCRequestMessage.model_validate(
                        json_message)
                except ValidationError as exc:
                    request_id = json_message.get("id")
                    await self._send_error(
                        request_id,
                        RPCError(
                            code=1000,
                            message="Invalid Request",
                            data=json.loads(exc.json()),
                        ),
                    )
                    continue
                await self.handle_request(request)
        except WebSocketDisconnect:
            pass
        finally:
            if self.topics:
                unsubscribe_client(self.app, self, list(self.topics))
                self.topics.clear()
            await self.stop_drainer()
            unregister_client(self.app, self)


async def install_module(app: FastAPI) -> None:

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        conn = WSServerConnection(app, ws)
        await conn.loop()
