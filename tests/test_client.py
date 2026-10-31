"""ModuleClient's message handling, driven through a fake MQTT client (no network)."""

from __future__ import annotations

import json
import ssl
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import paho.mqtt.client as mqtt
import pytest
from conftest import Certs
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode
from paho.mqtt.subscribeoptions import SubscribeOptions

import superset_module_sdk.client as client_module
from superset_module_sdk import (
    Flow,
    FlowOperation,
    ModuleClient,
    ModuleConfig,
    ModuleTwin,
    SdkError,
    SecretError,
    Telemetry,
    TelemetryError,
    TwinError,
)
from superset_module_sdk.messages import Command

COMMANDS = "input/directmethod/tenant-a/cluster-a/my-module"
REPLIES = "output/directmethod/tenant-a/cluster-a/my-module/res"
INPUT = "output/telemetry/tenant-a/node-a/sensor/v"
SCOPE = {"clusterId": "cluster-a", "moduleId": "my-module", "tenantConfigId": "tenant-a"}
TWIN: ModuleTwin = {"serverport": 4840, "variables": {"v": {"name": "Boiler"}}}


class Published:
    rc: int = mqtt.MQTT_ERR_SUCCESS


class FakeMqtt:
    """Just what ModuleClient uses of paho's client."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any], int, bool]] = []
        self.on_connect: Any = None
        self.on_connect_fail: Any = None
        self.on_disconnect: Any = None
        self.on_message: Any = None
        self.on_subscribe: Any = None
        self.connected: bool = True
        self.calls: list[tuple[str, Any]] = []
        self.subscriptions: list[Any] = []
        self.publish_rc: int = mqtt.MQTT_ERR_SUCCESS
        self.reconnect_delay: tuple[int, int] | None = None

    def publish(self, topic: str, payload: str, qos: int, retain: bool) -> Published:
        self.published.append((topic, json.loads(payload), qos, retain))
        result = Published()
        result.rc = self.publish_rc
        return result

    def is_connected(self) -> bool:
        return self.connected

    def tls_set_context(self, context: Any) -> None:
        self.calls.append(("tls", context))

    def connect_async(self, host: str, port: int, properties: Any = None) -> None:
        self.calls.append(("connect", (host, port, properties)))

    def loop_start(self) -> None:
        self.calls.append(("loop_start", None))

    def loop_stop(self) -> None:
        self.calls.append(("loop_stop", None))

    def disconnect(self) -> None:
        self.calls.append(("disconnect", None))

    def reconnect_delay_set(self, min_delay: int, max_delay: int) -> None:
        self.reconnect_delay = (min_delay, max_delay)

    def subscribe(self, topics: Any) -> None:
        self.subscriptions.append(topics)


@pytest.fixture
def fake() -> FakeMqtt:
    return FakeMqtt()


@pytest.fixture
def sdk(
    raw_config: dict[str, Any], fake: FakeMqtt, monkeypatch: pytest.MonkeyPatch
) -> ModuleClient:
    # Reloads run inline so the test sees their reply at once.
    monkeypatch.setattr(client_module, "_start_thread", lambda target: target())
    client = ModuleClient(ModuleConfig.parse(raw_config), cast(mqtt.Client, fake))
    client._subscribed = True  # connected and subscribed, as after a CONNACK and SUBACK
    return client


def command(name: str, correlation_id: str, payload: Any = None, **override: Any) -> bytes:
    now = int(time.time() * 1000)
    body = {
        "command": name,
        "correlationId": correlation_id,
        "delivery": "unicast",
        "expiresAtUnixMs": now + 5000,
        "issuedAtUnixMs": now,
        "payload": {} if payload is None else payload,
        "replyTopic": f"{REPLIES}/{correlation_id}",
        "target": SCOPE,
        **override,
    }
    return json.dumps(body).encode()


def fetches(monkeypatch: pytest.MonkeyPatch, outcome: Callable[..., ModuleTwin]) -> list[Any]:
    calls: list[Any] = []

    def fake_fetch(*args: Any) -> ModuleTwin:
        calls.append(args)
        return outcome()

    monkeypatch.setattr(client_module, "fetch_twin", fake_fetch)
    return calls


def test_reload_refetches_calls_the_handler_and_answers(
    sdk: ModuleClient, fake: FakeMqtt, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fetches(monkeypatch, lambda: TWIN)
    seen: list[ModuleTwin] = []
    sdk.on_twin_change(seen.append)
    sdk._handle(f"{COMMANDS}/unicast/reload", command("reload", "reload-1"), False)
    assert seen == [TWIN]
    assert calls[0][1:] == ("my-module", "reload-1")
    expected = {"correlationId": "reload-1", "result": {"reloaded": True}, "target": SCOPE}
    assert fake.published == [(f"{REPLIES}/reload-1", expected, 0, False)]


def failing() -> ModuleTwin:
    raise TwinError("HTTP 500")


def test_failed_reload_answers_reload_failed(
    sdk: ModuleClient, fake: FakeMqtt, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetches(monkeypatch, failing)
    seen: list[ModuleTwin] = []
    sdk.on_twin_change(seen.append)
    sdk._handle(f"{COMMANDS}/unicast/reload", command("reload", "r-2"), False)
    assert seen == []
    assert fake.published[0][1]["error"] == "reload_failed"


def test_failing_handler_answers_reload_failed(
    sdk: ModuleClient,
    fake: FakeMqtt,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fetches(monkeypatch, lambda: TWIN)
    later: list[ModuleTwin] = []

    def broken(twin: ModuleTwin) -> None:
        raise RuntimeError(f"bad variables {twin['serverport']}")

    sdk.on_twin_change(broken)
    sdk.on_twin_change(later.append)
    sdk._handle(f"{COMMANDS}/unicast/reload", command("reload", "r-3"), False)
    assert fake.published[0][1]["error"] == "reload_failed"
    assert later == []
    assert caplog.messages == ["twin change handler failed: RuntimeError"]
    assert "4840" not in caplog.text


def test_replies_only_while_connected(
    sdk: ModuleClient, fake: FakeMqtt, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetches(monkeypatch, lambda: TWIN)
    fake.connected = False
    sdk._handle(f"{COMMANDS}/unicast/reload", command("reload", "r-5"), False)
    sdk._handle(f"{COMMANDS}/unicast/reload", command("reload", "r-6", "text"), False)
    assert fake.published == []


def test_reload_with_a_non_object_payload(sdk: ModuleClient, fake: FakeMqtt) -> None:
    sdk._handle(f"{COMMANDS}/unicast/reload", command("reload", "r-4", "text"), False)
    assert fake.published[0][1] == {
        "correlationId": "r-4",
        "error": "invalid_payload",
        "target": SCOPE,
    }


def test_other_commands_get_no_answer(sdk: ModuleClient, fake: FakeMqtt) -> None:
    sdk._handle(
        f"{COMMANDS}/unicast/getValue", command("getValue", "g", {"variableId": "v"}), False
    )
    sdk._handle(f"{COMMANDS}/unicast/ping", command("ping", "p"), False)
    assert fake.published == []


def test_misaddressed_and_retained_reloads_get_no_answer(
    sdk: ModuleClient, fake: FakeMqtt, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fetches(monkeypatch, lambda: TWIN)
    topic = f"{COMMANDS}/unicast/reload"
    sdk._handle(topic, command("reload", "r", target={**SCOPE, "moduleId": "other"}), False)
    sdk._handle(topic, command("reload", "r"), True)
    assert calls == [] and fake.published == []


def test_inputs_reach_the_telemetry_handler(sdk: ModuleClient) -> None:
    seen: list[Telemetry] = []
    sdk.on_telemetry(seen.append)
    sdk._handle(INPUT, b'{"value": 1.5, "variableId": "v"}', False)
    sdk._handle(INPUT, b'{"value": 2, "variableId": "v"}', True)
    sdk._handle(INPUT, b'{"value": NaN, "variableId": "v"}', False)
    sdk._handle("output/telemetry/tenant-a/node-a/other/v", b'{"value": 3}', False)
    assert [(t.variable_id, t.value) for t in seen] == [("v", 1.5)]


def test_telemetry_handlers_run_in_order_and_failures_are_contained(
    sdk: ModuleClient, caplog: pytest.LogCaptureFixture
) -> None:
    order: list[str] = []

    def first(telemetry: Telemetry) -> None:
        order.append("first")
        raise RuntimeError(f"value {telemetry.value}")

    sdk.on_telemetry(first)
    sdk.on_telemetry(lambda _telemetry: order.append("second"))
    sdk._handle(INPUT, b'{"value": 4242.5, "variableId": "v"}', False)
    assert order == ["first", "second"]
    assert caplog.messages == ["telemetry handler failed: RuntimeError"]
    assert "4242.5" not in caplog.text


TELEMETRY = "output/telemetry/tenant-a/node-a/my-module/v"


def test_send_telemetry_needs_a_subscribed_connection(sdk: ModuleClient, fake: FakeMqtt) -> None:
    sdk._subscribed = False
    assert not sdk.send_telemetry("v", 1)  # before connect(): dropped, never queued
    sdk._subscribed = True
    fake.connected = False
    assert not sdk.send_telemetry("v", 1)
    assert fake.published == []
    with pytest.raises(TelemetryError, match="value"):
        sdk.send_telemetry("v", {"nested": 1})


def test_send_telemetry(sdk: ModuleClient, fake: FakeMqtt) -> None:
    sdk._subscribed = True
    assert sdk.send_telemetry("v", 21.5, name="Boiler", timestamp=7)
    expected = {"name": "Boiler", "timestamp": 7, "value": 21.5, "variableId": "v"}
    assert fake.published == [(TELEMETRY, expected, 0, False)]


def test_send_telemetry_names_from_the_last_twin(
    sdk: ModuleClient, fake: FakeMqtt, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdk._subscribed = True
    sdk.send_telemetry("v", 1, timestamp=7)  # no twin yet: no name
    fetches(monkeypatch, lambda: TWIN)
    sdk.get_twin()
    sdk.send_telemetry("v", 2, timestamp=7)  # the twin names v
    sdk.send_telemetry("w", 3, timestamp=7)  # w is not in the twin
    sdk.send_telemetry("v", 4, name="Given", timestamp=7)
    names = [message.get("name") for _, message, _, _ in fake.published]
    assert names == [None, "Boiler", None, "Given"]
    assert "name" not in fake.published[0][1]


def test_no_twin_configured(raw_config: dict[str, Any], fake: FakeMqtt) -> None:
    del raw_config["twin"]
    sdk = ModuleClient(ModuleConfig.parse(raw_config), cast(mqtt.Client, fake))
    with pytest.raises(TwinError, match="no module twin"):
        sdk.get_twin()
    raw_config["twin"] = None  # null is the same as absent
    sdk = ModuleClient(ModuleConfig.parse(raw_config), cast(mqtt.Client, fake))
    with pytest.raises(TwinError, match="no module twin"):
        sdk.get_twin()


def test_get_twin_refuses_a_non_string_correlation_id(sdk: ModuleClient) -> None:
    with pytest.raises(TwinError, match="correlation id"):
        sdk.get_twin(5)  # type: ignore[arg-type]


def test_secret_names(sdk: ModuleClient) -> None:
    assert sdk.secret_names() == ["device-password"]


def test_check_secrets(raw_config: dict[str, Any], fake: FakeMqtt, tmp_path: Path) -> None:
    good, bad = tmp_path / "a.json", tmp_path / "b.json"
    good.write_text('"value-a"')
    bad.write_text("value-b")
    raw_config["configuration"]["secretFiles"] = [str(good), str(bad)]
    sdk = ModuleClient(ModuleConfig.parse(raw_config), cast(mqtt.Client, fake))
    with pytest.raises(SecretError) as caught:
        sdk.check_secrets()
    assert str(caught.value) == "secret b cannot be read"
    bad.write_text('"value-b"')
    sdk.check_secrets()
    with pytest.raises(SecretError, match="secret name is not a string"):
        sdk.get_secret(5)  # type: ignore[arg-type]


@pytest.mark.parametrize("variable_id", [5, None, ["v"], {"v": 1}, "a/b"])
def test_invalid_variable_id_after_a_twin_fetch(
    sdk: ModuleClient, monkeypatch: pytest.MonkeyPatch, variable_id: Any
) -> None:
    fetches(monkeypatch, lambda: TWIN)
    sdk.get_twin()
    with pytest.raises(TelemetryError, match="variableId"):
        sdk.send_telemetry(variable_id, 1)


def test_twin_without_variables_names_nothing(
    sdk: ModuleClient, fake: FakeMqtt, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetches(monkeypatch, lambda: {"serverport": 1})
    sdk.get_twin()
    assert sdk.send_telemetry("v", 1, timestamp=1.7e12)
    assert fake.published[0][1] == {"timestamp": 1_700_000_000_000, "value": 1, "variableId": "v"}


# Connection life cycle ------------------------------------------------------------------------


@pytest.fixture
def tls_sdk(raw_config: dict[str, Any], certs: Certs, fake: FakeMqtt) -> ModuleClient:
    raw_config["mqtt"]["ca"] = certs.ca
    raw_config["mqtt"]["identity"] = certs.client
    return ModuleClient(ModuleConfig.parse(raw_config), cast(mqtt.Client, fake))


def test_connect_with_missing_certificates_logs_and_returns(
    raw_config: dict[str, Any], fake: FakeMqtt, caplog: pytest.LogCaptureFixture
) -> None:
    sdk = ModuleClient(ModuleConfig.parse(raw_config), cast(mqtt.Client, fake))
    sdk.connect()  # the files in raw_config do not exist here
    assert [kind for kind, _ in fake.calls] == ["tls", "connect", "loop_start"]
    assert caplog.messages == ["mqtt: certificates cannot be loaded: FileNotFoundError"]
    sdk._on_connect_fail(cast(mqtt.Client, None), None)  # paho retries: read again, logged
    assert caplog.messages[1:] == [
        "mqtt: connection failed",
        "mqtt: certificates cannot be loaded: FileNotFoundError",
    ]


def test_connect_twice_is_a_no_op_and_after_close_an_error(
    tls_sdk: ModuleClient, fake: FakeMqtt
) -> None:
    tls_sdk.connect()
    tls_sdk.connect()
    assert [kind for kind, _ in fake.calls] == ["tls", "connect", "loop_start"]
    tls_sdk.close()
    with pytest.raises(SdkError, match="closed"):
        tls_sdk.connect()


def test_connect_loads_certificates_and_starts_the_loop(
    tls_sdk: ModuleClient, fake: FakeMqtt
) -> None:
    tls_sdk.connect()
    kinds = [kind for kind, _ in fake.calls]
    assert kinds == ["tls", "connect", "loop_start"]
    host, port, properties = fake.calls[1][1]
    assert (host, port) == ("broker", 8883)
    assert properties.MaximumPacketSize == 2 * 1024 * 1024 + 1024
    assert isinstance(fake.calls[0][1], ssl.SSLContext)
    assert threading.stack_size() == 0  # restored after the loop thread starts


def test_on_connect_subscribes_to_commands_and_inputs(
    tls_sdk: ModuleClient, fake: FakeMqtt
) -> None:
    tls_sdk._on_connect(
        cast(mqtt.Client, fake), None, None, ReasonCode(PacketTypes.CONNACK, "Success"), None
    )
    [topics] = fake.subscriptions
    assert [topic for topic, _ in topics] == [f"{COMMANDS}/+/+", INPUT]
    options = topics[0][1]
    assert options.QoS == 1 and options.retainAsPublished
    assert options.retainHandling == SubscribeOptions.RETAIN_DO_NOT_SEND


def test_refused_connection_does_not_subscribe(
    tls_sdk: ModuleClient, fake: FakeMqtt, caplog: pytest.LogCaptureFixture
) -> None:
    refused = ReasonCode(PacketTypes.CONNACK, "Not authorized")
    tls_sdk._on_connect(cast(mqtt.Client, fake), None, None, refused, None)
    assert fake.subscriptions == []
    assert caplog.messages == ["mqtt: connection refused: Not authorized"]


def test_reconnects_every_five_seconds(tls_sdk: ModuleClient, fake: FakeMqtt) -> None:
    assert fake.reconnect_delay == (5, 5)
    assert client_module.RECONNECT_AFTER_S == 5


def test_real_paho_client_gets_the_fixed_delay(raw_config: dict[str, Any]) -> None:
    sdk = ModuleClient(ModuleConfig.parse(raw_config))
    assert sdk._client._reconnect_min_delay == 5 and sdk._client._reconnect_max_delay == 5


def test_is_connected_means_connected_and_subscribed(tls_sdk: ModuleClient, fake: FakeMqtt) -> None:
    assert not tls_sdk.is_connected()
    granted = [ReasonCode(PacketTypes.SUBACK, "Granted QoS 1")] * 2
    tls_sdk._on_subscribe(cast(mqtt.Client, fake), None, 1, granted, None)
    assert tls_sdk.is_connected()
    fake.connected = False
    assert not tls_sdk.is_connected()
    fake.connected = True
    refused = [
        ReasonCode(PacketTypes.SUBACK, "Granted QoS 1"),
        ReasonCode(PacketTypes.SUBACK, "Not authorized"),
    ]
    tls_sdk._on_subscribe(cast(mqtt.Client, fake), None, 2, refused, None)
    assert not tls_sdk.is_connected()


def test_refused_subscription_is_logged(
    tls_sdk: ModuleClient, fake: FakeMqtt, caplog: pytest.LogCaptureFixture
) -> None:
    granted = [ReasonCode(PacketTypes.SUBACK, "Granted QoS 1")]
    tls_sdk._on_subscribe(cast(mqtt.Client, fake), None, 1, granted, None)
    assert caplog.messages == []
    refused = [ReasonCode(PacketTypes.SUBACK, "Not authorized")]
    tls_sdk._on_subscribe(cast(mqtt.Client, fake), None, 2, refused, None)
    assert caplog.messages == ["mqtt: subscription refused"]


def test_disconnect_logs_and_reloads_certificates(
    tls_sdk: ModuleClient, caplog: pytest.LogCaptureFixture
) -> None:
    tls_sdk.connect()
    tls_sdk._subscribed = True
    tls_sdk._on_disconnect()
    assert not tls_sdk._subscribed
    assert "mqtt: disconnected, reconnecting" in caplog.messages
    tls_sdk._on_connect_fail(cast(mqtt.Client, None), None)


def test_close_is_quiet(
    tls_sdk: ModuleClient, fake: FakeMqtt, caplog: pytest.LogCaptureFixture
) -> None:
    tls_sdk.connect()
    tls_sdk.close()
    tls_sdk._on_disconnect()
    assert ("disconnect", None) in fake.calls and ("loop_stop", None) in fake.calls
    assert caplog.messages == []


def test_unreadable_certificates_are_logged(
    tls_sdk: ModuleClient, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    tls_sdk.connect()
    object.__setattr__(tls_sdk.config.mqtt, "identity", str(tmp_path / "gone.pem"))
    tls_sdk._on_disconnect()
    assert caplog.messages[-1] == "mqtt: certificates cannot be loaded: FileNotFoundError"


def test_on_message_never_raises(sdk: ModuleClient, caplog: pytest.LogCaptureFixture) -> None:
    def broken(*_: Any) -> None:
        raise RuntimeError("boom")

    sdk._handle = broken  # type: ignore[method-assign,assignment]
    message = mqtt.MQTTMessage(topic=INPUT.encode())
    message.payload = b"{}"
    sdk._on_message(cast(mqtt.Client, None), None, message)
    assert any("message ignored" in text for text in caplog.messages)


def test_publish_failure_returns_false(sdk: ModuleClient, fake: FakeMqtt) -> None:
    sdk._subscribed = True
    fake.publish_rc = mqtt.MQTT_ERR_NO_CONN
    assert not sdk.send_telemetry("v", 1)


def test_late_reply_is_not_sent(sdk: ModuleClient, fake: FakeMqtt) -> None:
    command = Command(
        name="reload", correlation_id="c", expires_at_ms=1, payload={}, reply_topic=f"{REPLIES}/c"
    )
    sdk._reply(command, {"result": {"reloaded": True}})
    assert fake.published == []


def test_reload_runs_on_a_thread(
    raw_config: dict[str, Any], fake: FakeMqtt, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetches(monkeypatch, lambda: TWIN)
    sdk = ModuleClient(ModuleConfig.parse(raw_config), cast(mqtt.Client, fake))
    sdk._subscribed = True
    done = threading.Event()
    sdk.on_twin_change(lambda _twin: done.set())
    sdk._handle(f"{COMMANDS}/unicast/reload", command("reload", "threaded"), False)
    assert done.wait(5)
    end = time.monotonic() + 5
    while not fake.published and time.monotonic() < end:
        time.sleep(0.01)
    assert fake.published[0][1]["result"] == {"reloaded": True}


def test_from_env_get_twin_and_get_secret(
    raw_config: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "device-password.json"
    secret.write_text('"value"')
    raw_config["configuration"]["secretFiles"] = [str(secret)]
    path = tmp_path / "module.json"
    path.write_text(json.dumps(raw_config))
    monkeypatch.setenv("MODULE_CONFIG", str(path))
    calls = fetches(monkeypatch, lambda: TWIN)
    sdk = ModuleClient.from_env()
    assert sdk.get_secret("device-password") == "value"
    assert sdk.get_twin() == TWIN
    assert calls[0][1:] == ("my-module", None)
    assert sdk.config.target.module_id == "my-module"


# Flow metrics ---------------------------------------------------------------------------------


def counts(sdk: ModuleClient, operation: str) -> tuple[int, int]:
    """(successes, errors) of one operation, read from metrics()."""
    found = dict(line.rsplit(" ", 1) for line in sdk.metrics().splitlines() if line[0] != "#")
    name = f'newboot_flow_operations_total{{operation="{operation}",outcome='
    return int(found[name + '"success"}']), int(found[name + '"error"}'])


def connected(sdk: ModuleClient) -> str:
    [line] = [line for line in sdk.metrics().splitlines() if line.startswith("newboot_flow_mqtt")]
    return line.rsplit(" ", 1)[1]


def test_metrics_render_the_client_flow(sdk: ModuleClient) -> None:
    assert isinstance(sdk.flow, Flow)
    sdk.flow.record(operation=FlowOperation.ACQUIRE, success=False)  # a module's own reads
    assert sdk.metrics() == sdk.flow.render()
    assert counts(sdk, "acquire") == (0, 1)
    assert connected(sdk) == "-1"  # nothing observed yet


def test_inputs_count_as_received_and_processed(sdk: ModuleClient) -> None:
    sdk.on_telemetry(lambda _telemetry: None)
    sdk._handle(INPUT, b'{"value": 1.5, "variableId": "v"}', False)
    sdk._handle(INPUT, b'{"value": NaN, "variableId": "v"}', False)
    sdk._handle(INPUT, b"not json", False)
    assert counts(sdk, "receive") == (3, 0)
    assert counts(sdk, "process") == (1, 2)


def test_a_failing_handler_is_a_processing_error(sdk: ModuleClient) -> None:
    def broken(_telemetry: Telemetry) -> None:
        raise RuntimeError("boom")

    sdk.on_telemetry(broken)
    sdk._handle(INPUT, b'{"value": 1, "variableId": "v"}', False)
    assert counts(sdk, "receive") == (1, 0)
    assert counts(sdk, "process") == (0, 1)


def test_retained_inputs_commands_and_other_topics_are_not_counted(
    sdk: ModuleClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetches(monkeypatch, lambda: TWIN)
    sdk._handle(INPUT, b'{"value": 2, "variableId": "v"}', True)
    sdk._handle("output/telemetry/tenant-a/node-a/other/v", b'{"value": 3}', False)
    sdk._handle(f"{COMMANDS}/unicast/reload", command("reload", "r-flow"), False)
    sdk._handle(f"{COMMANDS}/unicast/reload", command("reload", "r-bad", "text"), False)
    for operation in ("acquire", "receive", "process", "enqueue"):
        assert counts(sdk, operation) == (0, 0)


def test_sent_telemetry_counts_as_enqueued(sdk: ModuleClient, fake: FakeMqtt) -> None:
    assert sdk.send_telemetry("v", 1)
    fake.publish_rc = mqtt.MQTT_ERR_NO_CONN
    assert not sdk.send_telemetry("v", 2)  # refused by the MQTT client
    fake.connected = False
    assert not sdk.send_telemetry("v", 3)  # dropped while disconnected
    with pytest.raises(TelemetryError):
        sdk.send_telemetry("v", None)  # the caller's mistake, never sent: not counted
    assert counts(sdk, "enqueue") == (1, 2)
    assert counts(sdk, "process") == (0, 0)


def test_connection_metric_follows_is_connected(tls_sdk: ModuleClient, fake: FakeMqtt) -> None:
    assert connected(tls_sdk) == "-1"
    success = ReasonCode(PacketTypes.CONNACK, "Success")
    tls_sdk._on_connect(cast(mqtt.Client, fake), None, None, success, None)
    assert connected(tls_sdk) == "-1"  # connected, not yet subscribed
    granted = [ReasonCode(PacketTypes.SUBACK, "Granted QoS 1")]
    tls_sdk._on_subscribe(cast(mqtt.Client, fake), None, 1, granted, None)
    assert connected(tls_sdk) == "1"
    tls_sdk._on_disconnect()
    assert connected(tls_sdk) == "0"
    tls_sdk._on_subscribe(cast(mqtt.Client, fake), None, 2, granted, None)
    refused = [ReasonCode(PacketTypes.SUBACK, "Not authorized")]
    tls_sdk._on_subscribe(cast(mqtt.Client, fake), None, 3, refused, None)
    assert connected(tls_sdk) == "0"
    tls_sdk._on_subscribe(cast(mqtt.Client, fake), None, 4, granted, None)
    tls_sdk.close()
    assert connected(tls_sdk) == "0"


@pytest.mark.parametrize("failure", ["refused", "failed"])
def test_failed_connections_count_as_disconnected(
    tls_sdk: ModuleClient, fake: FakeMqtt, failure: str
) -> None:
    if failure == "refused":
        refused = ReasonCode(PacketTypes.CONNACK, "Not authorized")
        tls_sdk._on_connect(cast(mqtt.Client, fake), None, None, refused, None)
    else:
        tls_sdk._on_connect_fail(cast(mqtt.Client, fake), None)
    assert connected(tls_sdk) == "0"
