"""Pydantic wire models shared by every transport (HTTP, WS, raw UDS, proxy)."""
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

_TOPIC_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class ValueData(BaseModel):
    # Accept both the field name and the alias on input. The RPC result path
    # (rpc_handler.handle_request) serializes with default field names
    # (value_type/value_encoding), while ws/uds dump by alias (type/encoding).
    # Without populate_by_name, re-parsing a field-name payload silently falls
    # back to the defaults (text/plain), which mislabels base64/binary values
    # as plain text and causes double base64-encoding on the proxy initial
    # sync (clip.watch reply carries a raw ValueData).
    model_config = ConfigDict(populate_by_name=True)

    value: str
    value_type: Literal["text", "binary"] = Field(alias="type", default="text")
    value_encoding: Literal["plain", "hex", "base64"] = Field(alias="encoding",
                                                              default="plain")


class TopicData(BaseModel):
    topic: Annotated[str, Field(init=True)]
    value: Annotated[ValueData, Field(init=True)]
    meta: Annotated[dict[str, JsonValue], Field(init=True)]
    stub: bool = False          # value omitted — fetch via fetch_url
    fetch_url: str | None = None  # relative URL to retrieve full value

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @field_validator("topic")
    @classmethod
    def validate_topic(cls, v: str) -> str:
        if not _TOPIC_RE.match(v):
            raise ValueError(
                f"invalid topic name: {v!r} (must match {_TOPIC_RE.pattern})")
        return v


class DigestInfo(BaseModel):
    algo: str
    value: str


class ClipboardItem(BaseModel):
    topic: str
    value: JsonValue
    mime: str = "application/octet-stream"
    encoding: Literal["utf-8", "base64", "json"] = "base64"
    encrypted: bool = False
    size: int | None = None
    digest: DigestInfo | None = None

    @field_validator("topic")
    @classmethod
    def validate_topic(cls, v: str) -> str:
        if not _TOPIC_RE.match(v):
            raise ValueError(
                f"invalid topic name: {v!r} (must match {_TOPIC_RE.pattern})")
        return v


class KeyPublishParams(BaseModel):
    public_key: str
    label: str = ""
    # Admin token — required on the RPC (WS/UDS) transport where there is no
    # Authorization header; the HTTP endpoint keeps using the Bearer header.
    token: str = ""


class KeyPublishResult(BaseModel):
    ok: bool
    key_id: str


class KeyEntry(BaseModel):
    key_id: str
    public_key: str
    label: str


class KeysListResult(BaseModel):
    keys: list[KeyEntry]


class RPCError(BaseModel):
    code: int
    message: str
    data: JsonValue | None = None


class ClipPutParams(BaseModel):
    items: list[ClipboardItem]
    meta: dict[str, str] = Field(default_factory=dict)


class ClipPutResult(BaseModel):
    items: list[ClipboardItem]


class ClipGetParams(BaseModel):
    topic: str


class ClipGetResult(BaseModel):
    item: ClipboardItem


class ClipWatchParams(BaseModel):
    topics: list[str]
    public_key: str | None = None
    # Watcher's current UTC time, used for clock-offset exchange (conflict
    # resolution). Optional — older peers omit it.
    peer_now_utc: str | None = None


class ClipWatchResult(BaseModel):
    topics: list[str] = Field(default_factory=list)
    contents: dict[str, TopicData] = Field(default_factory=dict)
    # Server's UTC time at the moment it handled the watch, so the watcher can
    # estimate the clock offset. Omitted (None) keeps the field out of the wire.
    server_now_utc: str | None = None


class TopicsListParams(BaseModel):
    pass


class TopicsListResult(BaseModel):
    topics: list[str]


class HealthResult(BaseModel):
    ok: bool
    xsel_enabled: bool = False
    xsel_good: bool = False
    pasteboard_enabled: bool = False
    pasteboard_good: bool = False
    proxy_enabled: bool = False
    proxy_good: bool = False


class ProxyConnectParams(BaseModel):
    endpoint: str
    reconnect: bool = True


class ProxyConnectResult(BaseModel):
    ok: bool
    endpoint: str


class ProxyDisconnectResult(BaseModel):
    ok: bool


class TopicStatus(BaseModel):
    topic: str
    ts: str | None = None        # UTC ISO timestamp from meta["ts"]
    stub: bool = False           # True when only stub is stored locally
    size: int | None = None      # byte length of value (None when stub)
    stored_ago: float | None = None  # seconds since stored locally (monotonic)


class StatusResult(BaseModel):
    ok: bool
    topics: list[str] = Field(default_factory=list)
    topic_status: list[TopicStatus] = Field(default_factory=list)
    clients: list[str] = Field(default_factory=list)
    xsel: dict[str, JsonValue] = Field(default_factory=dict)
    pasteboard: dict[str, JsonValue] = Field(default_factory=dict)
    proxy: dict[str, JsonValue] = Field(default_factory=dict)
