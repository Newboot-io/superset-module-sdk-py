from __future__ import annotations

import json
from typing import Any

import pytest

from superset_module_sdk import ModuleConfig, TelemetryError
from superset_module_sdk.messages import (
    on_time,
    parse_command,
    parse_telemetry,
    reply_payload,
    telemetry_payload,
)
from superset_module_sdk.topics import (
    command_subscription,
    command_topic,
    reply_topic,
    telemetry_topic,
)

NOW = 1_800_000_000_000
COMMANDS = "input/directmethod/tenant-a/cluster-a/my-module"
REPLIES = "output/directmethod/tenant-a/cluster-a/my-module/res"
SCOPE = {"clusterId": "cluster-a", "moduleId": "my-module", "tenantConfigId": "tenant-a"}


@pytest.fixture
def config(raw_config: dict[str, Any]) -> ModuleConfig:
    return ModuleConfig.parse(raw_config)


def test_topics(config: ModuleConfig) -> None:
    assert telemetry_topic(config.target) == "output/telemetry/tenant-a/node-a/my-module/v"
    assert command_topic(config.target) == COMMANDS
    assert command_subscription(config.target) == COMMANDS + "/+/+"
    assert reply_topic(config.target) == REPLIES


def test_telemetry_payload_defaults() -> None:
    message = json.loads(telemetry_payload("variable-1", 21.5, timestamp=NOW))
    assert message == {  # no name given: none sent
        "timestamp": NOW,
        "value": 21.5,
        "variableId": "variable-1",
    }


def test_telemetry_payload_name_and_now() -> None:
    message = json.loads(telemetry_payload("variable-1", [1, 2], name="Boiler"))
    assert message["name"] == "Boiler" and message["value"] == [1, 2]
    assert isinstance(message["timestamp"], int) and message["timestamp"] > NOW - 10**12


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"variable_id": "a/b", "value": 1}, "variableId"),
        ({"variable_id": "", "value": 1}, "variableId"),
        ({"variable_id": 5, "value": 1}, "variableId"),
        ({"variable_id": "x" * 201, "value": 1}, "variableId"),
        ({"variable_id": "v", "value": float("nan")}, "value"),
        ({"variable_id": "v", "value": float("inf")}, "value"),
        ({"variable_id": "v", "value": [1.0, float("-inf")]}, "value"),
        ({"variable_id": "v", "value": object()}, "value"),
        ({"variable_id": "v", "value": None}, "value"),
        ({"variable_id": "v", "value": {"a": 1}}, "value"),
        ({"variable_id": "v", "value": [[1]]}, "value"),
        ({"variable_id": "v", "value": [{"a": 1}]}, "value"),
        ({"variable_id": "v", "value": 1, "timestamp": -1}, "timestamp"),
        ({"variable_id": "v", "value": 1, "timestamp": 2**53}, "timestamp"),
        ({"variable_id": "v", "value": 1, "timestamp": 1.5}, "timestamp"),
        ({"variable_id": "v", "value": 1, "timestamp": float("inf")}, "timestamp"),
        ({"variable_id": "v", "value": 1, "timestamp": "1"}, "timestamp"),
        ({"variable_id": "v", "value": 10**400}, "value"),
        ({"variable_id": "v", "value": [1, -(10**400)]}, "value"),
        ({"variable_id": "v\x00", "value": 1}, "variableId"),
        ({"variable_id": "v", "value": 1, "timestamp": 1.5}, "timestamp"),
        ({"variable_id": "v", "value": 1, "timestamp": True}, "timestamp"),
        ({"variable_id": "v", "value": 1, "name": 5}, "name"),
    ],
)
def test_telemetry_payload_refuses(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(TelemetryError, match=f"telemetry {message} is not valid"):
        telemetry_payload(**kwargs)


@pytest.mark.parametrize("value", [True, 0, -3, 2.5, 2**63, "text", "", [], [1, 2.5, "a", False]])
def test_telemetry_payload_accepts_scalars_and_arrays(value: Any) -> None:
    assert json.loads(telemetry_payload("v", value, timestamp=0))["value"] == value


INPUT = "output/telemetry/tenant-a/node-a/sensor/v"


def test_parse_telemetry_keeps_arrays_and_optional_fields() -> None:
    body = json.dumps({"value": [1, 2.5], "variableId": "v"}).encode()
    telemetry = parse_telemetry(INPUT, body, retain=False)
    assert telemetry is not None and telemetry.value == [1, 2.5]
    assert telemetry.name is None and telemetry.timestamp is None


def test_parse_telemetry_keeps_valid_messages() -> None:
    body = json.dumps({"name": "n", "timestamp": NOW, "value": 1.5, "variableId": "v"}).encode()
    telemetry = parse_telemetry(INPUT, body, retain=False)
    assert telemetry is not None
    assert (telemetry.variable_id, telemetry.name, telemetry.value) == ("v", "n", 1.5)
    assert telemetry.timestamp == NOW and telemetry.topic == INPUT


@pytest.mark.parametrize(
    ("body", "retain"),
    [
        (b'{"value": 1, "variableId": "v"}', True),
        (b'{"value": NaN, "variableId": "v"}', False),
        (b'{"value": 1e400, "variableId": "v"}', False),
        (b'{"value": {"a": [1]}, "variableId": "v"}', False),
        (b'{"value": [[1]], "variableId": "v"}', False),
        (b'{"value": null, "variableId": "v"}', False),
        (b'{"variableId": "v"}', False),
        (b'{"value": 1}', False),
        (b'{"value": 1, "variableId": "a/b"}', False),
        (b'{"value": 1, "variableId": "v", "timestamp": -5}', False),
        (b'{"value": 1, "variableId": "v", "timestamp": "now"}', False),
        (b'{"value": 1, "variableId": "v", "name": 3}', False),
        (b"not json", False),
        (b"[1]", False),
        (b"[" * 5000 + b"]" * 5000, False),
        (json.dumps({"value": "x" * 8192, "variableId": "v"}).encode(), False),
    ],
)
def test_parse_telemetry_drops(body: bytes, retain: bool) -> None:
    assert parse_telemetry(INPUT, body, retain) is None


def envelope(**override: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "command": "reload",
        "correlationId": "c-1",
        "delivery": "unicast",
        "expiresAtUnixMs": NOW + 5000,
        "issuedAtUnixMs": NOW,
        "payload": {},
        "replyTopic": f"{REPLIES}/c-1",
        "target": SCOPE,
    }
    fields.update(override)
    return {key: value for key, value in fields.items() if value is not None}


def parse(config: ModuleConfig, topic: str = f"{COMMANDS}/unicast/reload", **override: Any) -> Any:
    body = json.dumps(envelope(**override)).encode()
    return parse_command(topic, body, False, config.target, now=NOW)


def test_parse_command_accepts_an_addressed_command(config: ModuleConfig) -> None:
    command = parse(config, extra="ignored")
    assert command is not None
    assert command.name == "reload" and command.correlation_id == "c-1"
    assert command.reply_topic == f"{REPLIES}/c-1" and command.payload == {}
    assert parse(config, f"{COMMANDS}/broadcast/reload", delivery="broadcast") is not None


@pytest.mark.parametrize(
    "override",
    [
        {"target": {**SCOPE, "moduleId": "other"}},
        {"target": {**SCOPE, "extra": "x"}},
        {"replyTopic": f"{REPLIES}/elsewhere"},
        {"delivery": "multicast"},
        {"delivery": "broadcast"},  # does not match the topic
        {"command": "getValue"},  # does not match the topic
        {"correlationId": "a/b", "replyTopic": f"{REPLIES}/a/b"},
        {"expiresAtUnixMs": NOW - 1, "issuedAtUnixMs": NOW - 5000},
        {"expiresAtUnixMs": NOW + 40_000, "issuedAtUnixMs": NOW + 20_000},
        {"expiresAtUnixMs": NOW + 30_001},
        {"expiresAtUnixMs": None},
        {"expiresAtUnixMs": "soon"},
        {"expiresAtUnixMs": NOW + 5000.0},
    ],
)
def test_parse_command_ignores(config: ModuleConfig, override: dict[str, Any]) -> None:
    assert parse(config, **override) is None


def test_parse_command_ignores_retained_oversized_and_junk(config: ModuleConfig) -> None:
    topic = f"{COMMANDS}/unicast/reload"
    good = json.dumps(envelope()).encode()
    assert parse_command(topic, good, True, config.target, now=NOW) is None
    big = json.dumps(envelope(pad="x" * 8192)).encode()
    assert parse_command(topic, big, False, config.target, now=NOW) is None
    for junk in (b"5", b"null", b"not json", b'"text"', b"[1]"):
        assert parse_command(topic, junk, False, config.target, now=NOW) is None


def test_on_time() -> None:
    assert on_time(NOW, NOW + 30_000, now=NOW)
    assert on_time(NOW + 1000, NOW + 2000, now=NOW)  # 1 s of clock skew
    assert not on_time(NOW + 1001, NOW + 2000, now=NOW)
    assert not on_time(NOW, NOW, now=NOW)
    assert not on_time(True, NOW + 1, now=NOW)


def test_reply_payload(config: ModuleConfig) -> None:
    command = parse(config)
    body = json.loads(reply_payload(command, config.target, {"result": {"reloaded": True}}))
    assert body == {"correlationId": "c-1", "result": {"reloaded": True}, "target": SCOPE}


@pytest.mark.parametrize(
    ("given", "sent"), [(0, 0), (2**53 - 1, 2**53 - 1), (1.7e12, 1_700_000_000_000), (5.0, 5)]
)
def test_timestamps(given: Any, sent: int) -> None:
    body = telemetry_payload("v", 1, timestamp=given)
    assert json.loads(body)["timestamp"] == sent
    assert '"timestamp":%d,' % sent in body  # an integer on the wire, never 1.7e12


def test_received_timestamps_follow_the_same_rule() -> None:
    integral = parse_telemetry(
        INPUT, b'{"value": 1, "variableId": "v", "timestamp": 1.7e12}', False
    )
    assert integral is not None and integral.timestamp == 1_700_000_000_000
    for bad in (b"1.5", b"9007199254740992", b"-1"):
        body = b'{"value": 1, "variableId": "v", "timestamp": ' + bad + b"}"
        assert parse_telemetry(INPUT, body, False) is None
    huge = b'{"value": 1' + b"0" * 400 + b', "variableId": "v"}'
    assert parse_telemetry(INPUT, huge, False) is None


@pytest.mark.parametrize(
    "body",
    [
        b'{"value": 1, "variableId": "v", "name": null}',
        b'{"value": 1, "variableId": "v", "timestamp": null}',
        b'{"value": null, "variableId": "v"}',
        b'{"value": 1, "variableId": "v", "extra": NaN}',
        b'{"value": 1, "variableId": "v", "extra": [1e400]}',
        b'{"value": 1, "variableId": "v", "extra": {"n": 1' + b"0" * 400 + b"}}",
    ],
)
def test_parse_telemetry_drops_present_nulls_and_non_finite_anywhere(body: bytes) -> None:
    assert parse_telemetry(INPUT, body, False) is None


def test_parse_telemetry_keeps_absent_optional_fields() -> None:
    telemetry = parse_telemetry(INPUT, b'{"value": 1, "variableId": "v", "extra": null}', False)
    assert telemetry is not None and telemetry.name is None and telemetry.timestamp is None
