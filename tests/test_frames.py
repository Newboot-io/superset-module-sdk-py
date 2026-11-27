"""Image frames: frameInputs configuration, frame rules, send_frame and on_frame."""

from __future__ import annotations

from typing import Any, cast

import paho.mqtt.client as mqtt
import pytest
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode
from paho.mqtt.subscribeoptions import SubscribeOptions
from test_client import FakeMqtt

import superset_module_sdk.client as client_module
from superset_module_sdk import (
    ConfigError,
    FlowOperation,
    Frame,
    FrameError,
    ModuleClient,
    ModuleConfig,
)
from superset_module_sdk.messages import (
    MAX_FRAME_BYTES,
    frame_properties,
    parse_frame,
    valid_content_type,
)

CAMERA = "output/frames/tenant-a/node-b/camera"
OWN_FRAMES = "output/frames/tenant-a/node-a/my-module"
JPEG = b"\xff\xd8\xff\xe0 a jpeg \xff\xd9"


@pytest.fixture
def fake() -> FakeMqtt:
    return FakeMqtt()


@pytest.fixture
def frame_config(raw_config: dict[str, Any]) -> dict[str, Any]:
    raw_config["configuration"]["frameInputs"] = [CAMERA]
    return raw_config


@pytest.fixture
def sdk(
    frame_config: dict[str, Any], fake: FakeMqtt, monkeypatch: pytest.MonkeyPatch
) -> ModuleClient:
    monkeypatch.setattr(client_module, "_start_thread", lambda target: target())
    client = ModuleClient(ModuleConfig.parse(frame_config), cast(mqtt.Client, fake))
    client._subscribed = True  # connected and subscribed, as after a CONNACK and SUBACK
    return client


def publish_properties(content_type: str | None, user: list[tuple[str, str]]) -> Properties:
    properties = Properties(PacketTypes.PUBLISH)  # type: ignore[no-untyped-call]
    if content_type is not None:
        properties.ContentType = content_type
    if user:
        properties.UserProperty = user
    return properties


def operations(sdk: ModuleClient, operation: FlowOperation) -> tuple[int, int]:
    counters = sdk.flow._operations[operation]
    return counters.success, counters.error


# Configuration -------------------------------------------------------------------------------


def test_frame_inputs_default_to_none(raw_config: dict[str, Any]) -> None:
    assert ModuleConfig.parse(raw_config).frame_inputs == ()


def test_frame_inputs_are_read(frame_config: dict[str, Any]) -> None:
    assert ModuleConfig.parse(frame_config).frame_inputs == (CAMERA,)


@pytest.mark.parametrize(
    "topic",
    [
        "output/frames/tenant-b/node-b/camera",  # another tenant
        "output/telemetry/tenant-a/node-b/camera",
        "output/frames/tenant-a/node-b",
        "output/frames/tenant-a/node-b/camera/extra",
        "output/frames/tenant-a/+/camera",
        "output/frames/tenant-a/node-b/#",
        "output/frames/tenant-a/node-b/cam\x00era",
        "",
    ],
)
def test_frame_inputs_must_be_this_tenants_frame_topics(
    raw_config: dict[str, Any], topic: str
) -> None:
    raw_config["configuration"]["frameInputs"] = [topic]
    with pytest.raises(ConfigError, match="frameInputs"):
        ModuleConfig.parse(raw_config)


def test_frame_inputs_are_read_once_each(raw_config: dict[str, Any]) -> None:
    raw_config["configuration"]["frameInputs"] = [CAMERA, CAMERA]
    assert ModuleConfig.parse(raw_config).frame_inputs == (CAMERA,)


def test_frame_inputs_never_hold_the_modules_own_frames(raw_config: dict[str, Any]) -> None:
    raw_config["configuration"]["frameInputs"] = [OWN_FRAMES]
    with pytest.raises(ConfigError, match="own frame topic"):
        ModuleConfig.parse(raw_config)


def test_frame_inputs_must_be_a_list_of_strings(raw_config: dict[str, Any]) -> None:
    raw_config["configuration"]["frameInputs"] = CAMERA
    with pytest.raises(ConfigError, match="frameInputs"):
        ModuleConfig.parse(raw_config)


# Frame rules ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value", ["image/jpeg", "image/png", "application/octet-stream", "a/" + "b" * 125]
)
def test_plain_media_types_are_content_types(value: str) -> None:
    assert valid_content_type(value)


@pytest.mark.parametrize(
    "value",
    ["", "image", "image/", "/jpeg", "image/jpeg; q=1", "image /jpeg", 3, None, "a/" + "b" * 126],
)
def test_other_values_are_not_content_types(value: object) -> None:
    assert not valid_content_type(value)


def test_frame_properties_carry_the_frame_id_and_timestamp() -> None:
    assert frame_properties("image/jpeg", "f-1", 1_700_000_000_000) == [
        ("frameId", "f-1"),
        ("timestamp", "1700000000000"),
    ]


def test_frame_properties_stamp_now_without_a_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("superset_module_sdk.messages.now_ms", lambda: 42)
    assert frame_properties("image/png", None, None) == [("timestamp", "42")]


@pytest.mark.parametrize(
    ("content_type", "frame_id", "timestamp", "field"),
    [
        ("jpeg", None, None, "content type"),
        ("image/jpeg", "a/b", None, "frame id"),
        ("image/jpeg", "", None, "frame id"),
        ("image/jpeg", "\ud800", None, "frame id"),  # a lone surrogate is no UTF-8 text
        ("image/jpeg", None, -1, "timestamp"),
        ("image/jpeg", None, 1.5, "timestamp"),
    ],
)
def test_invalid_frame_metadata_is_refused(
    content_type: str, frame_id: str | None, timestamp: float | None, field: str
) -> None:
    with pytest.raises(FrameError, match=field):
        frame_properties(content_type, frame_id, timestamp)


def test_a_frames_repr_leaves_its_bytes_out() -> None:
    frame = Frame(CAMERA, "camera", b"\x00" * 1000, "image/jpeg", "f-1", 5)
    assert "data" not in repr(frame)
    assert "f-1" in repr(frame)


def test_a_frame_of_exactly_2_mib_is_kept() -> None:
    assert parse_frame(CAMERA, b"x" * MAX_FRAME_BYTES, False, None, []) is not None


def test_a_frame_is_parsed_with_its_metadata() -> None:
    frame = parse_frame(
        CAMERA, JPEG, False, "image/jpeg", [("frameId", "f-7"), ("timestamp", "1700000000123")]
    )
    assert frame == Frame(
        topic=CAMERA,
        module_id="camera",
        data=JPEG,
        content_type="image/jpeg",
        frame_id="f-7",
        timestamp=1_700_000_000_123,
    )


def test_a_frame_without_metadata_is_kept() -> None:
    frame = parse_frame(CAMERA, JPEG, False, None, [("other", "ignored")])
    assert frame is not None
    assert (frame.content_type, frame.frame_id, frame.timestamp) == (None, None, None)


@pytest.mark.parametrize(
    ("payload", "retain", "content_type", "user"),
    [
        (JPEG, True, "image/jpeg", []),
        (b"", False, "image/jpeg", []),
        (b"x" * (MAX_FRAME_BYTES + 1), False, "image/jpeg", []),
        (JPEG, False, "not a type", []),
        (JPEG, False, "image/jpeg", [("frameId", "a/b")]),
        (JPEG, False, "image/jpeg", [("frameId", "a"), ("frameId", "b")]),
        (JPEG, False, "image/jpeg", [("timestamp", "1"), ("timestamp", "2")]),
        (JPEG, False, "image/jpeg", [("timestamp", "-1")]),
        (JPEG, False, "image/jpeg", [("timestamp", "1e3")]),
        (JPEG, False, "image/jpeg", [("timestamp", str(2**53))]),
        (JPEG, False, "image/jpeg", [("timestamp", "9" * 5000)]),  # past int()'s digit limit
        (JPEG, False, "image/jpeg", [("timestamp", "0" * 17)]),
        (JPEG, False, "image/jpeg", [("timestamp", "١")]),  # an Arabic-Indic digit
    ],
)
def test_invalid_frames_are_dropped(
    payload: bytes, retain: bool, content_type: str, user: list[tuple[str, str]]
) -> None:
    assert parse_frame(CAMERA, payload, retain, content_type, user) is None


# send_frame ----------------------------------------------------------------------------------


def test_send_frame_publishes_the_bytes_and_their_properties(
    sdk: ModuleClient, fake: FakeMqtt
) -> None:
    assert sdk.send_frame(JPEG, "image/jpeg", frame_id="f-1", timestamp=1_700_000_000_000)
    [(topic, payload, qos, retain, properties)] = fake.frames
    assert (topic, payload, qos, retain) == (OWN_FRAMES, JPEG, 0, False)
    assert properties.ContentType == "image/jpeg"
    assert properties.UserProperty == [("frameId", "f-1"), ("timestamp", "1700000000000")]
    assert fake.published == []
    assert operations(sdk, FlowOperation.ENQUEUE) == (1, 0)


def test_send_frame_takes_bytearrays_and_memoryviews(sdk: ModuleClient, fake: FakeMqtt) -> None:
    assert sdk.send_frame(bytearray(JPEG), "image/jpeg")
    assert sdk.send_frame(memoryview(JPEG), "image/jpeg")
    assert [payload for _, payload, *_ in fake.frames] == [JPEG, JPEG]


@pytest.mark.parametrize("data", [b"", "text", None])
def test_send_frame_refuses_empty_or_non_byte_data(sdk: ModuleClient, data: Any) -> None:
    with pytest.raises(FrameError, match="frame data"):
        sdk.send_frame(data, "image/jpeg")


def test_send_frame_refuses_invalid_metadata(sdk: ModuleClient, fake: FakeMqtt) -> None:
    with pytest.raises(FrameError, match="content type"):
        sdk.send_frame(JPEG, "jpeg")
    assert fake.frames == []


def test_send_frame_refuses_a_frame_over_the_platform_limit(sdk: ModuleClient) -> None:
    with pytest.raises(FrameError, match="larger than the broker accepts"):
        sdk.send_frame(b"x" * MAX_FRAME_BYTES, "image/jpeg")


def test_send_frame_counts_the_topic_and_properties_in_the_packet(
    sdk: ModuleClient, fake: FakeMqtt
) -> None:
    """The broker's limit bounds the whole PUBLISH packet: a payload that fits alone but not with
    its topic and properties is refused."""
    sdk._broker_max_packet = 1000
    properties = publish_properties("image/jpeg", [("timestamp", "1")])
    # Measured with a payload long enough for the same two-byte remaining length.
    overhead = ModuleClient._packet_bytes(OWN_FRAMES, properties, b"x" * 500) - 500
    fits = b"x" * (1000 - overhead)
    assert ModuleClient._packet_bytes(OWN_FRAMES, properties, fits) == 1000
    assert sdk.send_frame(fits, "image/jpeg", timestamp=1)
    with pytest.raises(FrameError, match="larger than the broker accepts"):
        sdk.send_frame(fits + b"x", "image/jpeg", timestamp=1)


class ClosedSocket:
    """Stands for a socket paho never writes to; paho closes it when the client goes away."""

    def close(self) -> None:
        pass


def paho_packet(topic: str, properties: Properties, payload: bytes) -> bytes:
    """The PUBLISH packet paho itself encodes, caught where it would be queued for the socket
    (paho only needs a socket to be set; nothing is written)."""
    client = mqtt.Client(CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
    packets: list[bytes] = []

    def queue(_command: int, packet: bytes | bytearray, *_args: Any, **_kwargs: Any) -> int:
        packets.append(bytes(packet))
        return mqtt.MQTT_ERR_SUCCESS

    client._packet_queue = queue  # type: ignore[method-assign,assignment]
    client._sock = cast(Any, ClosedSocket())
    client._send_publish(0, topic.encode(), payload, 0, False, False, None, properties)
    return packets[0]


@pytest.mark.parametrize(
    # Around each remaining-length boundary: 127/128 and 16383/16384 bytes.
    "remaining",
    [64, 127, 128, 129, 16383, 16384, 16385, 2 * 1024 * 1024 - 200],
)
def test_packet_size_matches_what_paho_encodes(remaining: int) -> None:
    topic = "t"
    properties = publish_properties("image/jpeg", [("timestamp", "1")])
    overhead = 2 + len(topic) + len(properties.pack())  # type: ignore[no-untyped-call]
    assert remaining > overhead  # a real payload, not an empty one
    payload = b"x" * (remaining - overhead)
    assert len(paho_packet(topic, properties, payload)) == ModuleClient._packet_bytes(
        topic, properties, payload
    )


def test_send_frame_drops_frames_while_the_previous_ones_are_unwritten(
    sdk: ModuleClient, fake: FakeMqtt
) -> None:
    """A stalled connection: paho would queue QoS 0 frames without a bound."""
    fake.writes = False
    assert sdk.send_frame(JPEG, "image/jpeg")
    assert sdk.send_frame(JPEG, "image/jpeg")
    assert not sdk.send_frame(JPEG, "image/jpeg")
    assert len(fake.frames) == 2
    assert operations(sdk, FlowOperation.ENQUEUE) == (2, 1)
    fake.infos[0]._set_as_published()  # paho wrote the first: one slot frees up
    assert sdk.send_frame(JPEG, "image/jpeg")
    assert not sdk.send_frame(JPEG, "image/jpeg")
    assert len(fake.frames) == 3


def lose_connection(fake: FakeMqtt) -> None:
    """What paho's reconnect() does to the QoS 0 packets it had not written."""
    for info in fake.infos:
        if not info._published:
            info.rc = mqtt.MQTT_ERR_CONN_LOST
            info._set_as_published()


def test_frames_lost_in_a_reconnection_free_their_slots(sdk: ModuleClient, fake: FakeMqtt) -> None:
    fake.writes = False
    assert sdk.send_frame(JPEG, "image/jpeg")
    assert sdk.send_frame(JPEG, "image/jpeg")
    lose_connection(fake)
    with pytest.raises(RuntimeError):
        fake.infos[0].is_published()  # why the bound must not ask paho about a lost frame
    assert sdk.send_frame(JPEG, "image/jpeg")
    assert sdk.send_frame(JPEG, "image/jpeg")
    assert not sdk.send_frame(JPEG, "image/jpeg")


def test_a_new_connection_frees_every_slot(sdk: ModuleClient, fake: FakeMqtt) -> None:
    fake.writes = False
    assert sdk.send_frame(JPEG, "image/jpeg")
    assert sdk.send_frame(JPEG, "image/jpeg")
    connack(sdk, fake, None)
    assert sdk.send_frame(JPEG, "image/jpeg")


def test_a_refused_publish_takes_no_slot(sdk: ModuleClient, fake: FakeMqtt) -> None:
    fake.writes = False
    fake.publish_rc = mqtt.MQTT_ERR_NO_CONN
    for _ in range(3):
        assert not sdk.send_frame(JPEG, "image/jpeg")
    fake.publish_rc = mqtt.MQTT_ERR_SUCCESS
    assert sdk.send_frame(JPEG, "image/jpeg")


def test_send_frame_refuses_an_oversized_buffer_before_copying_it(
    sdk: ModuleClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_packet(*_args: Any) -> int:
        raise AssertionError("the size is checked before the packet is built")

    monkeypatch.setattr(ModuleClient, "_packet_bytes", staticmethod(no_packet))
    with pytest.raises(FrameError, match="larger than the broker accepts"):
        sdk.send_frame(bytearray(MAX_FRAME_BYTES + 1), "image/jpeg")


def test_send_frame_returns_false_when_not_connected(sdk: ModuleClient, fake: FakeMqtt) -> None:
    sdk._subscribed = False
    assert not sdk.send_frame(JPEG, "image/jpeg")
    assert fake.frames == []
    assert operations(sdk, FlowOperation.ENQUEUE) == (0, 1)


def test_send_frame_returns_false_when_the_publish_is_refused(
    sdk: ModuleClient, fake: FakeMqtt
) -> None:
    fake.publish_rc = mqtt.MQTT_ERR_NO_CONN
    assert not sdk.send_frame(JPEG, "image/jpeg")
    assert operations(sdk, FlowOperation.ENQUEUE) == (0, 1)


# Connection ----------------------------------------------------------------------------------


def connack(sdk: ModuleClient, fake: FakeMqtt, maximum_packet_size: int | None) -> None:
    properties = None
    if maximum_packet_size is not None:
        properties = Properties(PacketTypes.CONNACK)  # type: ignore[no-untyped-call]
        properties.MaximumPacketSize = maximum_packet_size
    success = ReasonCode(PacketTypes.CONNACK, "Success")
    sdk._on_connect(cast(mqtt.Client, fake), None, None, success, properties)


def test_frame_inputs_are_subscribed_at_qos_0(sdk: ModuleClient, fake: FakeMqtt) -> None:
    connack(sdk, fake, None)
    [subscriptions] = fake.subscriptions
    options = dict(subscriptions)
    assert options[CAMERA].QoS == 0
    assert options[CAMERA].retainHandling == SubscribeOptions.RETAIN_DO_NOT_SEND
    assert options["output/telemetry/tenant-a/node-a/sensor/v"].QoS == 1


def test_the_brokers_announced_limit_bounds_frames(sdk: ModuleClient, fake: FakeMqtt) -> None:
    connack(sdk, fake, 4096)
    with pytest.raises(FrameError, match="larger than the broker accepts"):
        sdk.send_frame(b"x" * 4096, "image/jpeg")
    connack(sdk, fake, None)  # a broker that announces none: the platform's 2 MiB again
    assert sdk.send_frame(b"x" * 4096, "image/jpeg")


def test_a_larger_broker_limit_keeps_the_platform_limit(sdk: ModuleClient, fake: FakeMqtt) -> None:
    connack(sdk, fake, 8 * MAX_FRAME_BYTES)
    with pytest.raises(FrameError, match="larger than the broker accepts"):
        sdk.send_frame(b"x" * MAX_FRAME_BYTES, "image/jpeg")


# on_frame ------------------------------------------------------------------------------------


def frame_message(topic: str, payload: bytes, properties: Properties | None) -> mqtt.MQTTMessage:
    message = mqtt.MQTTMessage(topic=topic.encode())
    message.payload = payload
    if properties is not None:
        message.properties = properties
    return message


def test_a_forwarded_frame_reaches_the_handlers(sdk: ModuleClient, fake: FakeMqtt) -> None:
    seen: list[Frame] = []
    sdk.on_frame(seen.append)
    properties = publish_properties("image/png", [("frameId", "f-3"), ("timestamp", "5")])
    sdk._on_message(cast(mqtt.Client, fake), None, frame_message(CAMERA, JPEG, properties))
    assert seen == [Frame(CAMERA, "camera", JPEG, "image/png", "f-3", 5)]
    assert operations(sdk, FlowOperation.RECEIVE) == (1, 0)
    assert operations(sdk, FlowOperation.PROCESS) == (1, 0)


def test_an_invalid_frame_is_received_but_not_processed(sdk: ModuleClient, fake: FakeMqtt) -> None:
    seen: list[Frame] = []
    sdk.on_frame(seen.append)
    properties = publish_properties("not a type", [])
    sdk._on_message(cast(mqtt.Client, fake), None, frame_message(CAMERA, JPEG, properties))
    assert seen == []
    assert operations(sdk, FlowOperation.RECEIVE) == (1, 0)
    assert operations(sdk, FlowOperation.PROCESS) == (0, 1)


def test_a_retained_frame_is_neither_received_nor_processed(sdk: ModuleClient) -> None:
    seen: list[Frame] = []
    sdk.on_frame(seen.append)
    sdk._handle(CAMERA, JPEG, True, None)
    assert seen == []
    assert operations(sdk, FlowOperation.RECEIVE) == (0, 0)


def test_a_frame_with_an_endless_timestamp_is_received_but_not_processed(
    sdk: ModuleClient, fake: FakeMqtt
) -> None:
    properties = publish_properties("image/jpeg", [("timestamp", "9" * 5000)])
    sdk._on_message(cast(mqtt.Client, fake), None, frame_message(CAMERA, JPEG, properties))
    assert operations(sdk, FlowOperation.RECEIVE) == (1, 0)
    assert operations(sdk, FlowOperation.PROCESS) == (0, 1)


def test_a_failing_frame_handler_is_logged_by_type_only(
    sdk: ModuleClient, caplog: pytest.LogCaptureFixture
) -> None:
    later: list[Frame] = []

    def broken(frame: Frame) -> None:
        raise RuntimeError(f"secret {frame.frame_id}")

    sdk.on_frame(broken)
    sdk.on_frame(later.append)
    sdk._handle(CAMERA, JPEG, False, publish_properties(None, [("frameId", "f-9")]))
    assert len(later) == 1
    assert "frame handler failed: RuntimeError" in caplog.text
    assert "f-9" not in caplog.text
    assert operations(sdk, FlowOperation.PROCESS) == (0, 1)


def test_a_frame_topic_that_is_also_an_input_is_a_frame(
    frame_config: dict[str, Any], fake: FakeMqtt
) -> None:
    frame_config["configuration"]["inputs"].append(CAMERA)
    client = ModuleClient(ModuleConfig.parse(frame_config), cast(mqtt.Client, fake))
    frames: list[Frame] = []
    client.on_frame(frames.append)
    client.on_telemetry(lambda telemetry: pytest.fail("a frame is not telemetry"))
    client._handle(CAMERA, JPEG, False, None)
    assert len(frames) == 1
