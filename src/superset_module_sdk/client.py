"""ModuleClient: the module twin, secrets, telemetry out and in, and the reload command."""

from __future__ import annotations

import logging
import socket
import ssl
import threading
from collections.abc import Callable
from typing import Any

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode
from paho.mqtt.subscribeoptions import SubscribeOptions

from ._json import valid_segment
from ._secret import read_secret, secret_names
from ._threads import deep_stack
from ._threads import start_thread as _start_thread
from .config import ModuleConfig
from .errors import SdkError, SecretError, TelemetryError, TwinError
from .flow import Flow, FlowOperation
from .messages import (
    INVALID_PAYLOAD,
    RELOAD,
    RELOAD_FAILED,
    Command,
    Telemetry,
    now_ms,
    parse_command,
    parse_telemetry,
    reply_payload,
    telemetry_payload,
)
from .topics import command_subscription, command_topic, telemetry_topic
from .twin import ModuleTwin, fetch_twin

log = logging.getLogger("superset_module_sdk")

# The largest packet the broker may send, announced on CONNECT. paho checks it only after reading a
# packet, so this relies on the broker (trusted, mutual TLS) honouring it.
MAX_PACKET_BYTES = 2 * 1024 * 1024 + 1024
# A fixed delay between connection attempts, the same in the three SDKs (paho would otherwise back
# off up to 120 s).
RECONNECT_AFTER_S = 5

TelemetryHandler = Callable[[Telemetry], None]
TwinHandler = Callable[[ModuleTwin], None]


class ModuleClient:
    """A module's connection to its superset.

    `get_twin()` and `get_secret()` work without `connect()`. `connect()` starts the MQTT
    connection in the background; `is_connected()` tells a health endpoint how it is, and
    `metrics()` gives its /metrics endpoint the flow metrics the client records. Use one client
    per process.
    """

    def __init__(self, config: ModuleConfig, mqtt_client: mqtt.Client | None = None) -> None:
        self.config = config
        self._telemetry_handlers: list[TelemetryHandler] = []
        self._twin_handlers: list[TwinHandler] = []
        self._reloading = threading.Lock()
        self._closing = False
        self._closed = False
        self._started = False
        self._subscribed = False
        self._telemetry_topic = telemetry_topic(config.target)
        self._command_prefix = command_topic(config.target) + "/"
        self._tls: ssl.SSLContext | None = None
        self._twin: ModuleTwin | None = None
        self._flow = Flow()
        if mqtt_client is None:
            client_id = f"{config.target.module_id}-{socket.gethostname()}"
            mqtt_client = mqtt.Client(
                CallbackAPIVersion.VERSION2, client_id=client_id, protocol=mqtt.MQTTv5
            )
        self._client = mqtt_client
        self._client.reconnect_delay_set(min_delay=RECONNECT_AFTER_S, max_delay=RECONNECT_AFTER_S)
        self._client.on_connect = self._on_connect
        self._client.on_connect_fail = self._on_connect_fail
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.on_subscribe = self._on_subscribe

    @classmethod
    def from_env(cls) -> ModuleClient:
        """A client for the configuration at MODULE_CONFIG (default /etc/newboot/module.json)."""
        return cls(ModuleConfig.from_env())

    # Module twin and secrets -------------------------------------------------------------

    def get_twin(self, correlation_id: str | None = None) -> ModuleTwin:
        """Fetch this module's twin: its fields and its optional `variables`, as received.
        Raises TwinError when no twin is configured or the fetch fails (no retry: call again
        later). A plain correlation id is sent as the x-newboot-correlation-id header."""
        if self.config.twin is None:
            raise TwinError("this module has no module twin")
        if correlation_id is not None and not isinstance(correlation_id, str):
            raise TwinError("correlation id is not a string")
        twin = fetch_twin(self.config.twin, self.config.target.module_id, correlation_id)
        self._twin = twin  # names the variables send_telemetry sends without one
        return twin

    def get_secret(self, name: str) -> str:
        """The value of a declared secret, read from its file on every call."""
        if not isinstance(name, str):
            raise SecretError("secret name is not a string")
        return read_secret(self.config.secret_files, name)

    def secret_names(self) -> list[str]:
        """The names of the secrets this module declares."""
        return secret_names(self.config.secret_files)

    def check_secrets(self) -> None:
        """Read every declared secret; raises SecretError naming the first unreadable one."""
        for name in self.secret_names():
            self.get_secret(name)

    # MQTT -----------------------------------------------------------------------------------

    def connect(self) -> None:
        """Start the connection to the broker in the background and return. paho reconnects
        when the connection drops, and the certificate files are read again before each attempt:
        missing or unreadable ones are logged and retried, never raised. A second call does
        nothing; a call after close() raises SdkError."""
        if self._closed:
            raise SdkError("the client is closed")
        if self._started:
            return
        self._started = True
        # Only the module's CA is trusted, never the system's.
        self._tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._tls.minimum_version = ssl.TLSVersion.TLSv1_2
        self._load_certificates()
        self._client.tls_set_context(self._tls)
        properties = Properties(PacketTypes.CONNECT)  # type: ignore[no-untyped-call]
        properties.MaximumPacketSize = MAX_PACKET_BYTES
        self._client.connect_async(
            self.config.mqtt.host, self.config.mqtt.port, properties=properties
        )
        with deep_stack():  # the loop thread parses received JSON
            self._client.loop_start()

    def is_connected(self) -> bool:
        """Connected and subscribed to the inputs and the command topic: what a module's
        /readyz should answer."""
        return self._subscribed and self._client.is_connected()

    @property
    def flow(self) -> Flow:
        """The flow metrics this client records: the MQTT connection, inputs received and
        processed, telemetry enqueued. A module that reads hardware records `acquire` here."""
        return self._flow

    def metrics(self) -> str:
        """The flow metrics in the Prometheus text format: what a module's /metrics should
        answer, with the content type METRICS_CONTENT_TYPE."""
        return self._flow.render()

    def send_telemetry(
        self,
        variable_id: str,
        value: Any,
        name: str | None = None,
        timestamp: int | float | None = None,
    ) -> bool:
        """Publish one value on this module's telemetry topic (QoS 0, not retained). Without a
        name, the variable's name in the last fetched twin is used, if it has one. Returns False
        when the client is not connected (also before connect()) or the MQTT client refuses the
        publish: the value is dropped and counted as an enqueue error."""
        if not valid_segment(variable_id):
            raise TelemetryError("telemetry variableId is not valid")
        if name is None and self._twin is not None:
            variables = self._twin.get("variables")
            variable = variables.get(variable_id) if isinstance(variables, dict) else None
            twin_name = variable.get("name") if isinstance(variable, dict) else None
            name = twin_name if isinstance(twin_name, str) else None
        payload = telemetry_payload(variable_id, value, name, timestamp)
        if not self.is_connected():
            self._flow.record(operation=FlowOperation.ENQUEUE, success=False)
            return False
        info = self._client.publish(self._telemetry_topic, payload, qos=0, retain=False)
        accepted = info.rc == mqtt.MQTT_ERR_SUCCESS
        self._flow.record(operation=FlowOperation.ENQUEUE, success=accepted)
        return accepted

    def on_telemetry(self, handler: TelemetryHandler) -> None:
        """Call `handler` for each message from a linked module (configuration.inputs)."""
        self._telemetry_handlers.append(handler)

    def on_twin_change(self, handler: TwinHandler) -> None:
        """Call `handler` with the twin fetched again when the superset sends `reload`."""
        self._twin_handlers.append(handler)

    def close(self) -> None:
        """Disconnect and stop the background loop. The client cannot connect again."""
        self._closing = True
        self._closed = True
        self._client.disconnect()
        self._client.loop_stop()
        self._subscribed = False
        self._flow.connection(False)

    # Callbacks -------------------------------------------------------------------------------

    def _load_certificates(self) -> None:
        # Certificates rotate: read the files again before every connection.
        if self._tls is None:
            return
        try:
            # Adds the rotated CA; the previous one stays trusted until the module restarts.
            self._tls.load_verify_locations(self.config.mqtt.ca)
            self._tls.load_cert_chain(self.config.mqtt.identity)
        except (OSError, ssl.SSLError) as error:  # the next attempt reads them again
            log.error("mqtt: certificates cannot be loaded: %s", type(error).__name__)

    def _on_connect_fail(self, _client: mqtt.Client, _userdata: Any) -> None:
        log.error("mqtt: connection failed")
        self._flow.connection(False)
        self._load_certificates()

    def _on_disconnect(self, *_: Any) -> None:
        self._subscribed = False
        self._flow.connection(False)
        if self._closing:
            return
        log.error("mqtt: disconnected, reconnecting")
        self._load_certificates()

    def _on_connect(
        self,
        client: mqtt.Client,
        _userdata: Any,
        _flags: Any,
        reason_code: ReasonCode,
        _properties: Properties | None,
    ) -> None:
        if reason_code.is_failure:
            log.error("mqtt: connection refused: %s", reason_code)
            self._flow.connection(False)
            return
        # The superset broker sends stored messages with the retain flag set, and they are
        # dropped; live ones arrive with the flag cleared. Other brokers skip stored ones.
        options = SubscribeOptions(
            qos=1, retainAsPublished=True, retainHandling=SubscribeOptions.RETAIN_DO_NOT_SEND
        )
        topics = [command_subscription(self.config.target), *self.config.inputs]
        client.subscribe([(topic, options) for topic in topics])

    def _on_subscribe(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        _mid: int,
        reason_codes: list[ReasonCode],
        _properties: Properties | None,
    ) -> None:
        self._subscribed = not any(code.is_failure for code in reason_codes)
        self._flow.connection(self._subscribed)  # what is_connected() answers from now on
        if not self._subscribed:
            log.error("mqtt: subscription refused")

    def _on_message(self, _client: mqtt.Client, _userdata: Any, message: mqtt.MQTTMessage) -> None:
        try:
            self._handle(message.topic, message.payload, bool(message.retain))
        except Exception as error:  # never let one bad message stop the MQTT loop
            log.error("message ignored: %s", type(error).__name__)

    def _handle(self, topic: str, payload: bytes, retain: bool) -> None:
        if topic in self.config.inputs:
            if retain:
                return  # a stored message, dropped on purpose: neither received nor processed
            self._flow.record(operation=FlowOperation.RECEIVE, success=True)
            telemetry = parse_telemetry(topic, payload, retain)
            processed = telemetry is not None
            if telemetry is not None:
                for handler in list(self._telemetry_handlers):
                    try:
                        handler(telemetry)
                    except Exception as error:  # its message may hold values: type only
                        log.error("telemetry handler failed: %s", type(error).__name__)
                        processed = False
            self._flow.record(operation=FlowOperation.PROCESS, success=processed)
            return
        if not topic.startswith(self._command_prefix):
            return
        command = parse_command(topic, payload, retain, self.config.target)
        # Only reload is handled in this version: other commands get no answer.
        if command is None or command.name != RELOAD:
            return
        if not isinstance(command.payload, dict):
            self._reply(command, {"error": INVALID_PAYLOAD})
            return
        _start_thread(lambda: self._reload(command))

    def _reload(self, command: Command) -> None:
        # One reload at a time.
        with self._reloading:
            try:
                twin = self.get_twin(command.correlation_id)
            except TwinError as error:  # the SDK's own message: no values
                log.error("reload: %s", error)
                self._reply(command, {"error": RELOAD_FAILED})
                return
            for handler in list(self._twin_handlers):
                try:
                    handler(twin)
                except Exception as error:  # its message may hold values: type only
                    log.error("twin change handler failed: %s", type(error).__name__)
                    self._reply(command, {"error": RELOAD_FAILED})
                    return
            self._reply(command, {"result": {"reloaded": True}})

    def _reply(self, command: Command, answer: dict[str, Any]) -> None:
        if now_ms() >= command.expires_at_ms:
            return  # nobody waits for a late answer
        if not self.is_connected():
            return  # never queued for later
        body = reply_payload(command, self.config.target, answer)
        self._client.publish(command.reply_topic, body, qos=0, retain=False)
