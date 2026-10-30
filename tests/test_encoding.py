"""Received JSON must be strict UTF-8 everywhere, as in the other SDKs: UTF-16, UTF-32, encoded
surrogates and a leading byte order mark are refused."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from superset_module_sdk import ConfigError, ModuleConfig, SecretError
from superset_module_sdk._json import parse_json
from superset_module_sdk._secret import read_secret
from superset_module_sdk.messages import parse_command, parse_telemetry
from superset_module_sdk.twin import validate_twin_answer

INPUT = "output/telemetry/tenant-a/node-a/sensor/v"
COMMAND_TOPIC = "input/directmethod/tenant-a/cluster-a/my-module/unicast/reload"
SCOPE = {"clusterId": "cluster-a", "moduleId": "my-module", "tenantConfigId": "tenant-a"}
BOM = b"\xef\xbb\xbf"
SURROGATE = b"\xed\xa0\x80"  # U+D800 encoded as if it were a character


def encodings(text: str) -> dict[str, bytes]:
    """The same JSON object text in each refused form."""
    return {
        "utf-16": text.encode("utf-16"),
        "utf-16-le": text.encode("utf-16-le"),
        "utf-32": text.encode("utf-32"),
        "bom": BOM + text.encode(),
        "surrogate": text.encode()[:-1] + b',"x":"' + SURROGATE + b'"}',
    }


def telemetry_text() -> str:
    return json.dumps({"value": 1, "variableId": "v"})


def command_text() -> str:
    now = int(time.time() * 1000)
    return json.dumps(
        {
            "command": "reload",
            "correlationId": "c",
            "delivery": "unicast",
            "expiresAtUnixMs": now + 5000,
            "issuedAtUnixMs": now,
            "payload": {},
            "replyTopic": "output/directmethod/tenant-a/cluster-a/my-module/res/c",
            "target": SCOPE,
        }
    )


def twin_text() -> str:
    return json.dumps({"modules": {"my-module": {"variables": {}}}})


def test_the_plain_utf8_forms_are_accepted() -> None:
    assert parse_telemetry(INPUT, telemetry_text().encode(), False) is not None
    target = ModuleConfig.parse(base_config()).target
    assert parse_command(COMMAND_TOPIC, command_text().encode(), False, target) is not None
    assert validate_twin_answer(parse_json(twin_text().encode()), "my-module") == {"variables": {}}


@pytest.mark.parametrize("form", ["utf-16", "utf-16-le", "utf-32", "bom", "surrogate"])
def test_telemetry_is_dropped(form: str) -> None:
    assert parse_telemetry(INPUT, encodings(telemetry_text())[form], False) is None


@pytest.mark.parametrize("form", ["utf-16", "utf-16-le", "utf-32", "bom", "surrogate"])
def test_command_is_ignored(form: str) -> None:
    target = ModuleConfig.parse(base_config()).target
    assert parse_command(COMMAND_TOPIC, encodings(command_text())[form], False, target) is None


@pytest.mark.parametrize("form", ["utf-16", "utf-16-le", "utf-32", "bom", "surrogate"])
def test_twin_answer_is_refused(form: str) -> None:
    with pytest.raises(ValueError):  # fetch_twin turns it into TwinError
        parse_json(encodings(twin_text())[form])


def test_python_json_on_bytes_would_have_accepted_them() -> None:
    """Why the strict decode exists: json.loads alone detects these encodings from bytes."""
    for form in ("utf-16", "utf-32", "bom"):
        assert json.loads(encodings(twin_text())[form]) == json.loads(twin_text())


@pytest.mark.parametrize("form", ["utf-16", "bom", "surrogate"])
def test_secret_file_is_refused(tmp_path: Path, form: str) -> None:
    path = tmp_path / "s.json"
    path.write_bytes(BOM + b'"value"' if form == "bom" else _secret_bytes(form))
    with pytest.raises(SecretError, match="^secret s cannot be read$"):
        read_secret([str(path)], "s")


def _secret_bytes(form: str) -> bytes:
    return '"value"'.encode("utf-16") if form == "utf-16" else b'"va' + SURROGATE + b'"'


def test_config_file_with_a_bom_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "module.json"
    path.write_bytes(BOM + json.dumps(base_config()).encode())
    with pytest.raises(ConfigError, match="not valid JSON"):
        ModuleConfig.from_file(str(path))


# Deep nesting from a small-stack thread, as a module's own threads may have on musl ------------


def on_small_stack(function: Callable[[], Any]) -> BaseException | None:
    errors: list[BaseException] = []

    def run() -> None:
        try:
            function()
        except BaseException as error:
            errors.append(error)

    previous = threading.stack_size(512 * 1024)
    try:
        thread = threading.Thread(target=run)
        thread.start()
    finally:
        threading.stack_size(previous)
    thread.join()
    return errors[0] if errors else None


DEEP = b"[" * 100_000 + b"]" * 100_000


def test_deep_secret_from_a_small_stack_thread(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_bytes(DEEP)
    error = on_small_stack(lambda: read_secret([str(path)], "s"))
    assert isinstance(error, SecretError) and str(error) == "secret s cannot be read"


def test_deep_config_from_a_small_stack_thread(tmp_path: Path) -> None:
    path = tmp_path / "module.json"
    path.write_bytes(DEEP)
    error = on_small_stack(lambda: ModuleConfig.from_file(str(path)))
    assert isinstance(error, ConfigError)


def base_config() -> dict[str, Any]:
    return {
        "brokerUrl": "tcps://broker:8883",
        "mqtt": {"ca": "/ca.crt", "identity": "/id.pem", "mode": "mutual-tls"},
        "target": {
            "clusterId": "cluster-a",
            "moduleId": "my-module",
            "nodeId": "node-a",
            "tenantConfigId": "tenant-a",
        },
    }
