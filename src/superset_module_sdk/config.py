"""The module configuration the superset mounts into the module (module.json)."""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ._json import parse_json, valid_segment
from ._threads import call_with_deep_stack
from .errors import ConfigError, UnsupportedModeError

CONFIG_ENV = "MODULE_CONFIG"
DEFAULT_CONFIG_PATH = "/etc/newboot/module.json"
DEFAULT_MQTT_PORT = 8883
MUTUAL_TLS = "mutual-tls"
SECRET_SUFFIX = ".json"
# The whole brokerUrl: scheme, a DNS name or a bracketed IPv6 literal, then an optional port.
# fullmatch, so a trailing newline is refused too.
_BROKER_URL = re.compile(r"(tcps|mqtts)://(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(?::([0-9]{1,5}))?")
TARGET_KEYS = ("clusterId", "moduleId", "nodeId", "tenantConfigId")


@dataclass(frozen=True)
class Target:
    """Where this module runs: its ids, which name its MQTT topics."""

    cluster_id: str
    module_id: str
    node_id: str
    tenant_config_id: str

    def scope(self) -> dict[str, str]:
        """The command and reply `target` object: cluster, module and tenant."""
        return {
            "clusterId": self.cluster_id,
            "moduleId": self.module_id,
            "tenantConfigId": self.tenant_config_id,
        }


@dataclass(frozen=True)
class MqttSettings:
    """The broker connection: a client certificate (cert, chain and key in one PEM) and a CA."""

    ca: str
    host: str
    identity: str
    port: int


@dataclass(frozen=True)
class TwinSettings:
    """The module twin endpoint, reached over HTTPS with a client certificate."""

    ca: str
    endpoint: str
    identity: str


@dataclass(frozen=True)
class ModuleConfig:
    """A validated module.json."""

    broker_url: str
    mqtt: MqttSettings
    target: Target
    twin: TwinSettings | None = None
    inputs: tuple[str, ...] = ()
    secret_files: tuple[str, ...] = ()
    configuration: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> ModuleConfig:
        """Read the file named by MODULE_CONFIG (default /etc/newboot/module.json)."""
        return cls.from_file(os.environ.get(CONFIG_ENV, DEFAULT_CONFIG_PATH))

    @classmethod
    def from_file(cls, path: str) -> ModuleConfig:
        try:
            with open(path, "rb") as file:
                data = file.read()
        except OSError as error:
            raise ConfigError(f"module configuration {path} cannot be read: {error}") from None
        try:
            # A deep-stack thread: nested JSON never crashes the caller's thread.
            raw = call_with_deep_stack(lambda: parse_json(data))
        except (ValueError, RecursionError):
            raise ConfigError(f"module configuration {path} is not valid JSON") from None
        return cls.parse(raw)

    @classmethod
    def parse(cls, raw: object) -> ModuleConfig:
        """Validate a decoded module.json."""
        root = _object(raw, "module configuration")
        mqtt = _object(root.get("mqtt"), "mqtt")
        mode = mqtt.get("mode")
        if mode != MUTUAL_TLS:
            raise UnsupportedModeError(f"this SDK connects with {MUTUAL_TLS} only, not {mode}")
        broker_url = _string(root.get("brokerUrl"), "brokerUrl")
        host, port = _broker(broker_url)
        target_raw = _object(root.get("target"), "target")
        for key in TARGET_KEYS:
            if not valid_segment(target_raw.get(key)):
                raise ConfigError(f"target.{key} is missing or not a valid topic level")
        target = Target(
            cluster_id=target_raw["clusterId"],
            module_id=target_raw["moduleId"],
            node_id=target_raw["nodeId"],
            tenant_config_id=target_raw["tenantConfigId"],
        )
        twin = None
        if root.get("twin") is not None:
            twin_raw = _object(root["twin"], "twin")
            endpoint = _string(twin_raw.get("endpoint"), "twin.endpoint")
            try:
                parsed = urlsplit(endpoint)
                valid = parsed.scheme == "https" and bool(parsed.hostname)
                parsed.port  # noqa: B018 (raises ValueError for an invalid port)
            except ValueError:
                valid = False
            if not valid:
                raise ConfigError("twin.endpoint is not an https URL")
            twin = TwinSettings(
                ca=_string(twin_raw.get("ca"), "twin.ca"),
                endpoint=endpoint,
                identity=_string(twin_raw.get("identity"), "twin.identity"),
            )
        configuration = _object(root.get("configuration", {}), "configuration")
        return cls(
            broker_url=broker_url,
            mqtt=MqttSettings(
                ca=_string(mqtt.get("ca"), "mqtt.ca"),
                host=host,
                identity=_string(mqtt.get("identity"), "mqtt.identity"),
                port=port,
            ),
            target=target,
            twin=twin,
            inputs=_inputs(configuration.get("inputs", [])),
            secret_files=_secret_files(configuration.get("secretFiles", [])),
            configuration=configuration,
        )


def _object(value: object, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{what} is not an object")
    return value


def _string(value: object, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{what} is missing or not a string")
    return value


def _broker(url: str) -> tuple[str, int]:
    """Exactly scheme://host[:port], matched on the raw string (no URL library, which would strip
    or normalise characters first): scheme tcps or mqtts, a DNS name or a bracketed IPv6 literal,
    port 1-65535 (8883 when absent)."""
    match = _BROKER_URL.fullmatch(url)
    if match is None:
        raise ConfigError("brokerUrl must be tcps:// or mqtts:// followed by host[:port] only")
    host, port = match.group(2), match.group(3)
    if host.startswith("["):
        host = host[1:-1]
    if port is None:
        return host, DEFAULT_MQTT_PORT
    if not 1 <= int(port) <= 65535:
        raise ConfigError("brokerUrl has an invalid port")
    return host, int(port)


def _has_control(text: str) -> bool:
    return any(unicodedata.category(c) == "Cc" for c in text)


def _inputs(value: object) -> tuple[str, ...]:
    inputs = _strings(value, "configuration.inputs")
    if any("+" in topic or "#" in topic for topic in inputs):
        raise ConfigError("configuration.inputs must not contain + or #")
    if any(_has_control(topic) for topic in inputs):
        raise ConfigError("configuration.inputs must not contain control characters")
    return inputs


def _secret_files(value: object) -> tuple[str, ...]:
    files = _strings(value, "configuration.secretFiles")
    if not all(path.endswith(SECRET_SUFFIX) for path in files):
        raise ConfigError("configuration.secretFiles entries must end in .json")
    return files


def _strings(value: object, what: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ConfigError(f"{what} is not a list of strings")
    return tuple(value)
