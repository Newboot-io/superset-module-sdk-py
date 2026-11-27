"""Pure message rules: telemetry payloads, linked-module input, image frames, command envelopes."""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import Any

from ._json import dump_json, is_int, parse_json, valid_segment
from .config import Target
from .errors import FrameError, TelemetryError
from .topics import command_topic, reply_topic

DELIVERIES = ("broadcast", "unicast")
# The superset's frame contract: one frame per message, at most the broker's 2 MiB packet.
MAX_FRAME_BYTES = 2 * 1024 * 1024
FRAME_ID_PROPERTY = "frameId"
FRAME_TIMESTAMP_PROPERTY = "timestamp"
# A plain type/subtype media type, such as image/jpeg: no parameters, at most 127 characters.
_CONTENT_TYPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*")
MAX_CONTENT_TYPE_LENGTH = 127
MAX_CLOCK_SKEW_MS = 1000
MAX_MESSAGE_BYTES = 8192
MAX_TTL_MS = 30_000
RELOAD = "reload"
INVALID_PAYLOAD = "invalid_payload"
RELOAD_FAILED = "reload_failed"


def now_ms() -> int:
    return int(time.time() * 1000)


MAX_TIMESTAMP_MS = 2**53 - 1


def is_scalar(value: object) -> bool:
    """A number finite as an IEEE-754 double, a string or a boolean."""
    if isinstance(value, bool | str):
        return True
    if isinstance(value, int):
        try:
            return math.isfinite(float(value))
        except OverflowError:  # beyond the double range
            return False
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def timestamp_ms(value: object) -> int | None:
    """An integer from 0 to 2^53-1, or None. A float with an integral value counts."""
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        value = int(value)
    if is_int(value) and isinstance(value, int) and 0 <= value <= MAX_TIMESTAMP_MS:
        return value
    return None


def valid_value(value: object) -> bool:
    """A scalar, or an array of scalars (no objects, no nested arrays, no NaN or infinity)."""
    return is_scalar(value) or (isinstance(value, list) and all(map(is_scalar, value)))


def telemetry_error(message: dict[str, Any]) -> str | None:
    """The first field of a telemetry message that breaks the payload rules, or None. The same
    rules apply when sending and when receiving."""
    if not valid_segment(message.get("variableId")):
        return "variableId"
    if "value" not in message or not valid_value(message["value"]):
        return "value"
    # Optional fields may be absent; present, they must be valid (null is not).
    if "timestamp" in message and timestamp_ms(message["timestamp"]) is None:
        return "timestamp"
    if "name" in message and not isinstance(message["name"], str):
        return "name"
    return None


def telemetry_payload(
    variable_id: str, value: Any, name: str | None = None, timestamp: int | float | None = None
) -> str:
    """One telemetry message: variableId, value, timestamp (epoch ms, now when not given) and
    name (left out when not given). Raises TelemetryError naming the field that is invalid."""
    message: dict[str, Any] = {
        "timestamp": now_ms() if timestamp is None else timestamp,
        "value": value,
        "variableId": variable_id,
    }
    if name is not None:
        message["name"] = name
    field = telemetry_error(message)
    if field is not None:
        raise TelemetryError(f"telemetry {field} is not valid")
    message["timestamp"] = timestamp_ms(message["timestamp"])  # 1.7e12 is sent as 1700000000000
    return dump_json(message)


@dataclass(frozen=True)
class Telemetry:
    """A message from a module the superset links to this one."""

    topic: str
    variable_id: str
    value: Any
    name: str | None
    timestamp: int | None


def parse_telemetry(topic: str, payload: bytes, retain: bool) -> Telemetry | None:
    """A linked module's message, or None: retained messages, those over 8 KiB, non-JSON, and
    messages that break the payload rules are dropped."""
    if retain or len(payload) > MAX_MESSAGE_BYTES:
        return None
    try:
        message = parse_json(payload)
    except (ValueError, RecursionError):
        return None
    if not isinstance(message, dict) or telemetry_error(message) is not None:
        return None
    return Telemetry(
        topic=topic,
        variable_id=message["variableId"],
        value=message["value"],
        name=message.get("name"),
        timestamp=timestamp_ms(message.get("timestamp")),
    )


def valid_content_type(value: object) -> bool:
    """A plain type/subtype media type, such as image/jpeg."""
    return (
        isinstance(value, str)
        and len(value) <= MAX_CONTENT_TYPE_LENGTH
        and _CONTENT_TYPE.fullmatch(value) is not None
    )


def frame_metadata(
    content_type: str, frame_id: str | None, timestamp: int | float | None
) -> tuple[str, list[tuple[str, str]]]:
    """The content type and user properties of a frame to send: frameId when given, and
    timestamp (epoch ms, now when not given). Raises FrameError naming the field that is
    invalid."""
    if not valid_content_type(content_type):
        raise FrameError("frame content type is not a type/subtype media type")
    properties: list[tuple[str, str]] = []
    if frame_id is not None:
        if not valid_segment(frame_id):
            raise FrameError("frame id is not valid")
        properties.append((FRAME_ID_PROPERTY, frame_id))
    stamp = timestamp_ms(now_ms() if timestamp is None else timestamp)
    if stamp is None:
        raise FrameError("frame timestamp is not valid")
    properties.append((FRAME_TIMESTAMP_PROPERTY, str(stamp)))
    return content_type, properties


@dataclass(frozen=True)
class Frame:
    """An image frame from a module the superset forwards to this one (configuration.frameInputs).
    `data` holds the image bytes as sent; `module_id` names the module that sent it."""

    topic: str
    module_id: str
    data: bytes
    content_type: str | None
    frame_id: str | None
    timestamp: int | None


def _frame_timestamp(text: str) -> int | None:
    # Decimal digits only: no sign, no space, no exponent.
    return timestamp_ms(int(text)) if text.isascii() and text.isdigit() else None


def parse_frame(
    topic: str,
    payload: bytes,
    retain: bool,
    content_type: object,
    user_properties: list[tuple[str, str]],
) -> Frame | None:
    """A forwarded frame, or None: retained, empty and oversized frames are dropped, as are
    frames whose content type, frameId or timestamp is invalid or given twice. Other user
    properties are ignored."""
    if retain or not 0 < len(payload) <= MAX_FRAME_BYTES:
        return None
    if content_type is not None and not valid_content_type(content_type):
        return None
    values: dict[str, list[str]] = {}
    for name, value in user_properties:
        values.setdefault(name, []).append(value)
    frame_ids = values.get(FRAME_ID_PROPERTY, [])
    stamps = values.get(FRAME_TIMESTAMP_PROPERTY, [])
    if len(frame_ids) > 1 or len(stamps) > 1:
        return None
    frame_id = frame_ids[0] if frame_ids else None
    if frame_id is not None and not valid_segment(frame_id):
        return None
    stamp = _frame_timestamp(stamps[0]) if stamps else None
    if stamps and stamp is None:
        return None
    return Frame(
        topic=topic,
        module_id=topic.rsplit("/", 1)[-1],
        data=bytes(payload),
        content_type=content_type if isinstance(content_type, str) else None,
        frame_id=frame_id,
        timestamp=stamp,
    )


@dataclass(frozen=True)
class Command:
    """A command addressed to this module and on time."""

    name: str
    correlation_id: str
    expires_at_ms: int
    payload: Any
    reply_topic: str


def on_time(issued: object, expires: object, now: int | None = None) -> bool:
    """Issued now (1 s of clock skew allowed), valid at most 30 s, not yet expired."""
    current = now_ms() if now is None else now
    return (
        is_int(issued)
        and is_int(expires)
        and isinstance(issued, int)
        and isinstance(expires, int)
        and issued <= current + MAX_CLOCK_SKEW_MS
        and expires > current
        and expires > issued
        and expires - issued <= MAX_TTL_MS
    )


def parse_command(
    topic: str, payload: bytes, retain: bool, target: Target, now: int | None = None
) -> Command | None:
    """A command addressed to this module and on time, or None (it gets no answer). Unknown keys
    are ignored, which stays compatible with a longer envelope; the target must match exactly."""
    if retain or len(payload) > MAX_MESSAGE_BYTES:
        return None
    try:
        message = parse_json(payload)
    except (ValueError, RecursionError):
        return None
    if not isinstance(message, dict):
        return None
    name, correlation_id = message.get("command"), message.get("correlationId")
    delivery = message.get("delivery")
    replies = reply_topic(target)
    addressed = (
        valid_segment(correlation_id)
        and valid_segment(name)
        and delivery in DELIVERIES
        and topic == f"{command_topic(target)}/{delivery}/{name}"
        and message.get("replyTopic") == f"{replies}/{correlation_id}"
        and message.get("target") == target.scope()
    )
    issued, expires = message.get("issuedAtUnixMs"), message.get("expiresAtUnixMs")
    if not addressed or not on_time(issued, expires, now) or not isinstance(expires, int):
        return None
    assert isinstance(name, str) and isinstance(correlation_id, str)
    return Command(
        name=name,
        correlation_id=correlation_id,
        expires_at_ms=expires,
        payload=message.get("payload"),
        reply_topic=f"{replies}/{correlation_id}",
    )


def reply_payload(command: Command, target: Target, answer: dict[str, Any]) -> str:
    """The reply holds exactly correlationId, target and a result or a short error code."""
    return dump_json({"correlationId": command.correlation_id, "target": target.scope(), **answer})
