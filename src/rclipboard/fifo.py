from __future__ import annotations

import asyncio as a
import base64
import binascii
import contextlib
import os
from logging import Logger
from pathlib import Path
from typing import Any

import aiofiles as af
from fastapi import FastAPI

from rclipboard.app_state import enqueue_request_topics, enqueue_topic_data
from rclipboard.helpers import (
    clipboard_item_to_topic_data,
    fifo_dir_from_env,
    topic_data_to_clipboard_item,
)
from rclipboard.log import get_logger
from rclipboard.types import (
    ClipboardItem,
    ClipGetResult,
    ClipPutParams,
    HealthResult,
    StatusResult,
    TopicData,
    TopicsListResult,
)
from rclipboard.xsel import get_xsel_status

logger: Logger = get_logger(__name__)
error = logger.error
warning = logger.warning
info = logger.info
debug = logger.debug

DEFAULT_TOPICS = ["c", "p", "s"]
DEFAULT_TOPICS_SET = set(DEFAULT_TOPICS)


_topic_data_to_clipboard_item = topic_data_to_clipboard_item
_clipboard_item_to_topic_data = clipboard_item_to_topic_data


def _topic_data_to_bytes(data: TopicData) -> bytes:
    value = data.value.value
    if data.value.value_encoding == "plain":
        return value.encode("utf-8")
    if data.value.value_encoding == "base64":
        return base64.b64decode(value)
    if data.value.value_encoding == "hex":
        return bytes.fromhex(value)
    raise ValueError(f"unsupported value encoding: {data.value.value_encoding}")


def _raw_bytes_to_topic_data(topic: str, payload: bytes) -> TopicData:
    return TopicData.model_validate({
        "topic": topic,
        "meta": {
            "app": "fifo"
        },
        "value": {
            "value": base64.b64encode(payload).decode("ascii"),
            "type": "binary",
            "encoding": "base64",
        },
    })


def _topic_paths(root: Path, topic: str) -> dict[str, Path]:
    return {
        "put_raw": root / f"put.{topic}.fifo",
        "put_json": root / f"put.{topic}.fifo.json",
        "state_raw": root / f"state.{topic}",
        "state_json": root / f"state.{topic}.json",
    }


def _snapshot_paths(root: Path) -> dict[str, Path]:
    return {
        "topics": root / "topics.json",
        "status": root / "status.json",
        "health": root / "health.json",
    }


async def _write_atomic(path: Path, payload: bytes) -> None:
    tmp: Path = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    info(f"path: '{path}', tmp: '{tmp}' content: '{payload.decode('utf-8')}'")

    def _owner_only(p, flags):
        return os.open(p, flags, 0o600)

    async with af.open(tmp, "w+b", opener=_owner_only) as f:
        await f.write(payload)
    tmp.chmod(0o400)
    os.replace(tmp, path)


def _ensure_fifo(path: Path) -> None:
    if path.exists():
        if not path.is_fifo():
            raise RuntimeError(f"{path} exists and is not a fifo")
        return
    os.mkfifo(path, 0o600)


async def _read_fifo_bytes(path: Path) -> bytes:
    async with af.open(path, "rb") as f:
        return await f.read()


class FIFOTransport:

    def __init__(self, app: FastAPI, root: Path):
        self.app = app
        self.root = root
        info(f"fifo root: {root}")
        self.reader_tasks: dict[str, tuple[a.Task[None], a.Task[None]]] = {}
        self.snapshot_cache: dict[str, bytes] = {}

    async def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for topic in DEFAULT_TOPICS:
            self.ensure_topic(topic)
        await self.refresh_snapshots()

    async def stop(self) -> None:
        for raw_task, json_task in self.reader_tasks.values():
            raw_task.cancel()
            json_task.cancel()
            with contextlib.suppress(a.CancelledError):
                await raw_task
            with contextlib.suppress(a.CancelledError):
                await json_task
        self.reader_tasks.clear()

    def ensure_topic(self, topic: str) -> None:
        if topic not in DEFAULT_TOPICS_SET:
            debug(f"fifo ignored unsupported topic: {topic}")
            return
        if topic in self.reader_tasks:
            return
        paths = _topic_paths(self.root, topic)
        _ensure_fifo(paths["put_raw"])
        _ensure_fifo(paths["put_json"])
        self.reader_tasks[topic] = (
            a.create_task(self.raw_reader_loop(topic), name=f"fifo_raw_{topic}"),
            a.create_task(self.json_reader_loop(topic), name=f"fifo_json_{topic}"),
        )

    async def raw_reader_loop(self, topic: str) -> None:
        path = _topic_paths(self.root, topic)["put_raw"]
        while True:
            payload = await _read_fifo_bytes(path)
            if not payload:
                continue
            try:
                await enqueue_topic_data(
                    self.app,
                    _raw_bytes_to_topic_data(topic, payload),
                    source=None,
                )
            except Exception as exc:
                debug(
                    f"fifo raw put failed for topic={topic}: {exc}",
                    exc_info=True,
                )

    async def json_reader_loop(self, topic: str) -> None:
        path = _topic_paths(self.root, topic)["put_json"]
        while True:
            payload = await _read_fifo_bytes(path)
            if not payload:
                continue
            try:
                params = ClipPutParams.model_validate_json(payload.decode("utf-8"))
                if len(params.items) != 1:
                    raise ValueError("fifo json put expects exactly one item")
                item = params.items[0]
                if item.topic != topic:
                    raise ValueError(f"fifo json put topic mismatch: {item.topic} != {topic}")
                await enqueue_topic_data(
                    self.app,
                    _clipboard_item_to_topic_data(item, meta=params.meta),
                    source=None,
                )
            except Exception as exc:
                debug(
                    f"fifo json put failed for topic={topic}: {exc}",
                    exc_info=True,
                )

    async def refresh_snapshots(self) -> None:
        topics = list(sorted((await enqueue_request_topics(self.app)) or []))
        snapshots = _snapshot_paths(self.root)
        await self._write_snapshot_if_changed(
            "topics",
            snapshots["topics"],
            TopicsListResult(topics=topics).model_dump_json().encode("utf-8"),
        )
        xsel = get_xsel_status(self.app)
        await self._write_snapshot_if_changed(
            "health",
            snapshots["health"],
            HealthResult(
                ok=True,
                xsel_enabled=bool(xsel["enabled"]),
                xsel_good=bool(xsel["good"]),
            ).model_dump_json().encode("utf-8"),
        )
        clients = [client.name for client in getattr(self.app.state.main, "clients", [])]
        await self._write_snapshot_if_changed(
            "status",
            snapshots["status"],
            StatusResult(
                ok=True,
                topics=topics,
                clients=clients,
                xsel=xsel,
            ).model_dump_json().encode("utf-8"),
        )

    async def _write_snapshot_if_changed(
        self,
        name: str,
        path: Path,
        payload: bytes,
    ) -> None:
        if self.snapshot_cache.get(name) == payload:
            return
        await _write_atomic(path, payload)
        self.snapshot_cache[name] = payload

    async def on_topic_data(self, _app: FastAPI, topic_data: TopicData, _source: Any) -> None:
        if topic_data.topic not in DEFAULT_TOPICS_SET:
            debug(f"fifo ignored state update for unsupported topic: {topic_data.topic}")
            return
        self.ensure_topic(topic_data.topic)
        paths = _topic_paths(self.root, topic_data.topic)
        info(f"paths: {paths}")
        try:
            await _write_atomic(paths["state_raw"], _topic_data_to_bytes(topic_data))
            await _write_atomic(
                paths["state_json"],
                ClipGetResult(item=_topic_data_to_clipboard_item(
                    topic_data)).model_dump_json().encode("utf-8"),
            )
            await self.refresh_snapshots()
        except (ValueError, binascii.Error) as exc:
            warning(f"fifo snapshot update failed for topic={topic_data.topic}: {exc}")

    async def on_runtime_state_change(self, _app: FastAPI, _reason: str) -> None:
        await self.refresh_snapshots()


def install_fifo(app: FastAPI) -> None:
    fifo_dir = fifo_dir_from_env()
    app.state.fifo_enabled = bool(fifo_dir)
    app.state.fifo_root = fifo_dir
    app.state.fifo_transport = None
    app.state.fifo_start_task = None
    if not fifo_dir:
        return
    transport = FIFOTransport(app, Path(fifo_dir))
    app.state.fifo_transport = transport
    app.state.local_topic_data_hooks.append(transport.on_topic_data)
    app.state.runtime_state_hooks.append(transport.on_runtime_state_change)
    app.state.fifo_start_task = a.create_task(transport.start(), name="fifo_start")


async def shutdown_fifo(app: FastAPI) -> None:
    start_task: a.Task[None] | None = getattr(app.state, "fifo_start_task", None)
    if start_task:
        with contextlib.suppress(a.CancelledError):
            await start_task
        app.state.fifo_start_task = None
    transport: FIFOTransport | None = getattr(app.state, "fifo_transport", None)
    if transport is None:
        return
    await transport.stop()
    app.state.fifo_transport = None
