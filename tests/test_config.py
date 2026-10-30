from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from superset_module_sdk import ConfigError, ModuleConfig, UnsupportedModeError


def test_parses_a_rendered_module_json(raw_config: dict[str, Any]) -> None:
    config = ModuleConfig.parse(raw_config)
    assert config.mqtt.host == "broker"
    assert config.mqtt.port == 8883
    assert config.target.module_id == "my-module"
    assert config.inputs == ("output/telemetry/tenant-a/node-a/sensor/v",)
    assert config.secret_files == ("/vault/material/device-password.json",)
    assert config.twin is not None and config.twin.endpoint == "https://twin/twin"


def test_default_port_and_optional_parts(raw_config: dict[str, Any]) -> None:
    raw_config["brokerUrl"] = "mqtts://broker.example"
    del raw_config["twin"], raw_config["configuration"]
    config = ModuleConfig.parse(raw_config)
    assert config.mqtt.port == 8883
    assert config.twin is None
    assert config.inputs == () and config.secret_files == ()


def test_only_mutual_tls(raw_config: dict[str, Any]) -> None:
    raw_config["mqtt"]["mode"] = "token"
    with pytest.raises(UnsupportedModeError, match="mutual-tls only, not token"):
        ModuleConfig.parse(raw_config)


@pytest.mark.parametrize("value", ["", "a/b", "a+", "#", "x" * 201, 5, None])
def test_target_ids_must_be_topic_levels(raw_config: dict[str, Any], value: object) -> None:
    raw_config["target"]["moduleId"] = value
    with pytest.raises(ConfigError, match="target.moduleId"):
        ModuleConfig.parse(raw_config)


def test_twin_endpoint_must_be_https(raw_config: dict[str, Any]) -> None:
    raw_config["twin"]["endpoint"] = "http://twin/twin"
    with pytest.raises(ConfigError, match="https"):
        ModuleConfig.parse(raw_config)


def test_inputs_must_be_strings(raw_config: dict[str, Any]) -> None:
    raw_config["configuration"]["inputs"] = ["ok", 3]
    with pytest.raises(ConfigError, match="configuration.inputs"):
        ModuleConfig.parse(raw_config)


def test_broker_needs_a_host(raw_config: dict[str, Any]) -> None:
    raw_config["brokerUrl"] = "tcps://:8883"
    with pytest.raises(ConfigError, match="brokerUrl"):
        ModuleConfig.parse(raw_config)


def test_from_env(
    tmp_path: Path, raw_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "module.json"
    path.write_text(json.dumps(raw_config))
    monkeypatch.setenv("MODULE_CONFIG", str(path))
    assert ModuleConfig.from_env().target.node_id == "node-a"


def test_unreadable_files(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot be read"):
        ModuleConfig.from_file(str(tmp_path / "missing.json"))
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    with pytest.raises(ConfigError, match="not valid JSON"):
        ModuleConfig.from_file(str(bad))


@pytest.mark.parametrize(
    ("path", "message"),
    [
        (("brokerUrl",), "brokerUrl"),
        (("mqtt",), "mqtt is not an object"),
        (("mqtt", "ca"), "mqtt.ca"),
        (("mqtt", "identity"), "mqtt.identity"),
        (("mqtt", "mode"), "mutual-tls only"),
        (("target",), "target is not an object"),
        (("target", "nodeId"), "target.nodeId"),
        (("target", "tenantConfigId"), "target.tenantConfigId"),
        (("target", "clusterId"), "target.clusterId"),
        (("twin", "ca"), "twin.ca"),
        (("twin", "endpoint"), "twin.endpoint"),
        (("twin", "identity"), "twin.identity"),
    ],
)
def test_missing_fields(raw_config: dict[str, Any], path: tuple[str, ...], message: str) -> None:
    parent = raw_config
    for key in path[:-1]:
        parent = parent[key]
    del parent[path[-1]]
    with pytest.raises(ConfigError, match=message):
        ModuleConfig.parse(raw_config)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("configuration", [], "configuration is not an object"),
        ("twin", "https://twin", "twin is not an object"),
        ("brokerUrl", "tcps://broker:99999", "invalid port"),
        ("brokerUrl", 5, "brokerUrl"),
    ],
)
def test_wrong_types(raw_config: dict[str, Any], key: str, value: object, message: str) -> None:
    raw_config[key] = value
    with pytest.raises(ConfigError, match=message):
        ModuleConfig.parse(raw_config)


def test_not_an_object() -> None:
    with pytest.raises(ConfigError, match="module configuration is not an object"):
        ModuleConfig.parse([])


def test_secret_files_must_be_strings(raw_config: dict[str, Any]) -> None:
    raw_config["configuration"]["secretFiles"] = "/vault/material/a.json"
    with pytest.raises(ConfigError, match="configuration.secretFiles"):
        ModuleConfig.parse(raw_config)


def test_extra_configuration_is_kept(raw_config: dict[str, Any]) -> None:
    raw_config["configuration"]["interval"] = 10
    assert ModuleConfig.parse(raw_config).configuration["interval"] == 10


@pytest.mark.parametrize("url", ["tcps://broker", "mqtts://broker:1883"])
def test_broker_schemes(raw_config: dict[str, Any], url: str) -> None:
    raw_config["brokerUrl"] = url
    assert ModuleConfig.parse(raw_config).mqtt.host == "broker"


@pytest.mark.parametrize("url", ["tcp://broker:1883", "mqtt://broker", "https://broker"])
def test_plain_broker_schemes_are_refused(raw_config: dict[str, Any], url: str) -> None:
    raw_config["brokerUrl"] = url
    with pytest.raises(ConfigError, match="tcps:// or mqtts://"):
        ModuleConfig.parse(raw_config)


@pytest.mark.parametrize("topic", ["output/telemetry/+/node-a/sensor/v", "output/#"])
def test_inputs_must_not_be_wildcards(raw_config: dict[str, Any], topic: str) -> None:
    raw_config["configuration"]["inputs"] = [topic]
    with pytest.raises(ConfigError, match=r"must not contain \+ or #"):
        ModuleConfig.parse(raw_config)


def test_secret_files_must_end_in_json(raw_config: dict[str, Any]) -> None:
    raw_config["configuration"]["secretFiles"] = ["/vault/material/device-password"]
    with pytest.raises(ConfigError, match=r"must end in \.json"):
        ModuleConfig.parse(raw_config)


@pytest.mark.parametrize(
    ("url", "port"),
    [
        ("tcps://broker", 8883),
        ("mqtts://broker:1", 1),
        ("tcps://broker:65535", 65535),
        ("tcps://192.0.2.1:8883", 8883),
        ("tcps://[::1]:8883", 8883),
    ],
)
def test_broker_ports(raw_config: dict[str, Any], url: str, port: int) -> None:
    raw_config["brokerUrl"] = url
    assert ModuleConfig.parse(raw_config).mqtt.port == port


@pytest.mark.parametrize(
    "url",
    [
        "tcps://broker:0",
        "tcps://broker:65536",
        "tcps://broker:",
        "tcps://broker:x",
        "tcps://user@broker",
        "tcps://user:pass@broker:8883",
        "tcps://broker/",
        "tcps://broker/path",
        "tcps://broker?x=1",
        "tcps://broker#frag",
        "tcps://broker;params",
        "tcps://bro ker",
        "tcps://",
    ],
)
def test_broker_url_is_scheme_host_port_only(raw_config: dict[str, Any], url: str) -> None:
    raw_config["brokerUrl"] = url
    with pytest.raises(ConfigError, match="brokerUrl"):
        ModuleConfig.parse(raw_config)


@pytest.mark.parametrize("key", ["clusterId", "moduleId", "nodeId", "tenantConfigId"])
def test_target_ids_refuse_control_characters(raw_config: dict[str, Any], key: str) -> None:
    raw_config["target"][key] = "a\nb"
    with pytest.raises(ConfigError, match=f"target.{key}"):
        ModuleConfig.parse(raw_config)


@pytest.mark.parametrize("topic", ["output/telemetry/a\x00/v", "output/\x7f", "output/\x1b[0m"])
def test_inputs_refuse_control_characters(raw_config: dict[str, Any], topic: str) -> None:
    raw_config["configuration"]["inputs"] = [topic]
    with pytest.raises(ConfigError, match="control characters"):
        ModuleConfig.parse(raw_config)


def test_twin_null_means_no_twin(raw_config: dict[str, Any]) -> None:
    raw_config["twin"] = None
    assert ModuleConfig.parse(raw_config).twin is None


def test_unsupported_mode_in_the_platform_shape() -> None:
    """The mode is checked first: a token-mode configuration has no scheme in brokerUrl and no
    identity, and still raises UnsupportedModeError."""
    raw = {
        "brokerUrl": "other-broker:18884",
        "mqtt": {"ca": "/etc/newboot/ca/mqtt-ca.crt", "mode": "token"},
        "target": {
            "clusterId": "cluster-a",
            "moduleId": "my-module",
            "nodeId": "node-a",
            "tenantConfigId": "tenant-a",
        },
    }
    with pytest.raises(UnsupportedModeError, match="not token"):
        ModuleConfig.parse(raw)


# The brokerUrl table shared by the three SDKs, verbatim.
BROKER_ACCEPT = [
    ("tcps://broker", "broker", 8883),
    ("mqtts://broker:8883", "broker", 8883),
    ("tcps://mq.example.com:1", "mq.example.com", 1),
    ("tcps://10.0.0.1:65535", "10.0.0.1", 65535),
    ("tcps://[::1]:8883", "::1", 8883),
    ("tcps://[::1]", "::1", 8883),
    ("tcps://[1.2.3.4]", "1.2.3.4", 8883),
]
BROKER_REFUSE = [
    "tcp://broker:8883",
    "tcps://broker:",
    "tcps://broker:0",
    "tcps://broker:65536",
    "tcps://broker:+8883",
    "tcps://broker:8883/",
    "tcps://broker:8883/x",
    "tcps://u:p@broker:8883",
    "tcps://broker:8883?x=1",
    "tcps://broker:8883#f",
    "tcps://a_b:8883",
    "tcps://b;x:8883",
    "tcps://h%41:8883",
    "tcps://bröker:8883",
    "tcps://broker\u0000:8883",
    "tcps://:8883",
    "tcps://[::1:8883",
    "broker:8883",
    "tcps://broker:8883\n",
    "tcps://bro\tker:8883",
    " tcps://broker",
    "tcps://broker:8883 ",
]


@pytest.mark.parametrize(("url", "host", "port"), BROKER_ACCEPT)
def test_broker_url_table_accepts(
    raw_config: dict[str, Any], url: str, host: str, port: int
) -> None:
    raw_config["brokerUrl"] = url
    mqtt = ModuleConfig.parse(raw_config).mqtt
    assert (mqtt.host, mqtt.port) == (host, port)


@pytest.mark.parametrize("url", BROKER_REFUSE)
def test_broker_url_table_refuses(raw_config: dict[str, Any], url: str) -> None:
    raw_config["brokerUrl"] = url
    with pytest.raises(ConfigError, match="brokerUrl"):
        ModuleConfig.parse(raw_config)


@pytest.mark.parametrize(
    ("path", "message"),
    [
        (("mqtt", "ca"), "mqtt.ca"),
        (("mqtt", "identity"), "mqtt.identity"),
        (("twin", "ca"), "twin.ca"),
        (("twin", "identity"), "twin.identity"),
    ],
)
def test_paths_must_not_be_empty(
    raw_config: dict[str, Any], path: tuple[str, str], message: str
) -> None:
    raw_config[path[0]][path[1]] = ""
    with pytest.raises(ConfigError, match=message):
        ModuleConfig.parse(raw_config)


def test_secret_file_paths_must_not_be_empty(raw_config: dict[str, Any]) -> None:
    raw_config["configuration"]["secretFiles"] = [""]
    with pytest.raises(ConfigError, match="configuration.secretFiles"):
        ModuleConfig.parse(raw_config)


@pytest.mark.parametrize("mode", [None, 1, ["mutual-tls"], "Mutual-TLS"])
def test_missing_or_non_string_mode_is_unsupported(
    raw_config: dict[str, Any], mode: object
) -> None:
    if mode is None:
        del raw_config["mqtt"]["mode"]
    else:
        raw_config["mqtt"]["mode"] = mode
    with pytest.raises(UnsupportedModeError):
        ModuleConfig.parse(raw_config)


def test_deeply_nested_config_file(tmp_path: Path) -> None:
    path = tmp_path / "module.json"
    path.write_text("[" * 100_000 + "]" * 100_000)
    with pytest.raises(ConfigError, match="not valid JSON"):
        ModuleConfig.from_file(str(path))


@pytest.mark.parametrize(
    "endpoint", ["https://twin:99999/twin", "https://twin:x/twin", "https://[::1/twin"]
)
def test_twin_endpoint_with_an_invalid_url(raw_config: dict[str, Any], endpoint: str) -> None:
    raw_config["twin"]["endpoint"] = endpoint
    with pytest.raises(ConfigError, match="twin.endpoint is not an https URL"):
        ModuleConfig.parse(raw_config)
