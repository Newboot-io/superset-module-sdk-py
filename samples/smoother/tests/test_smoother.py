"""The Smoother's wiring, with a fake client in place of the SDK's connection."""

from __future__ import annotations

from typing import Any, cast

from smoother import Smoother

from superset_module_sdk import ModuleClient, Telemetry

INPUT = "output/telemetry/tenant-a/node-a/sensor/v"
TWIN = {
    "window": 2,
    "variables": {"variable-avg": {"name": "temp_avg"}, "variable-f": {"name": "temp_f"}},
}


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []

    def send_telemetry(self, variable_id: str, value: Any) -> bool:
        self.sent.append((variable_id, value))
        return True


def reading(name: str, value: Any) -> Telemetry:
    return Telemetry(topic=INPUT, variable_id="variable-t", value=value, name=name, timestamp=0)


def test_sends_average_and_fahrenheit_for_each_temperature() -> None:
    fake = FakeClient()
    smoother = Smoother(cast(ModuleClient, fake))
    smoother.apply(TWIN)
    smoother.on_telemetry(reading("temperature", 20))
    smoother.on_telemetry(reading("pressure", 1013))
    smoother.on_telemetry(reading("temperature", 22))
    smoother.on_telemetry(reading("temperature", 30))
    assert fake.sent == [
        ("variable-avg", 20.0),
        ("variable-f", 68.0),
        ("variable-avg", 21.0),
        ("variable-f", 69.8),
        ("variable-avg", 26.0),  # window 2: 22 and 30
        ("variable-f", 78.8),
    ]


def test_reload_changes_window_and_variables() -> None:
    fake = FakeClient()
    smoother = Smoother(cast(ModuleClient, fake))
    smoother.apply(TWIN)
    smoother.apply({"window": "bad", "variables": {"variable-avg": {"name": "temp_avg"}}})
    assert smoother.average.window == 5
    smoother.on_telemetry(reading("temperature", 10))
    assert fake.sent == [("variable-avg", 10.0)]
