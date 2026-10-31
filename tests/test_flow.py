"""Flow: the common module flow metrics, checked against the platform's conformance vector."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from superset_module_sdk import METRICS_CONTENT_TYPE, Flow, FlowOperation

# Ported from Newboot's reference implementation of the module flow-metrics contract and
# contributed by Newboot under Apache-2.0; conformance vector at revision 589be4b6, identical in
# every SDK. Every SDK and module must pass it.
CONFORMANCE = Path(__file__).with_name("flow-conformance.json")

# What Newboot's reference implementation of the contract renders for the state built in
# test_renders_like_the_platform_reference.
REFERENCE = """\
# TYPE newboot_flow_mqtt_connected gauge
newboot_flow_mqtt_connected 1
# TYPE newboot_flow_operations_total counter
newboot_flow_operations_total{operation="acquire",outcome="success"} 0
newboot_flow_operations_total{operation="acquire",outcome="error"} 0
newboot_flow_operations_total{operation="receive",outcome="success"} 1
newboot_flow_operations_total{operation="receive",outcome="error"} 0
newboot_flow_operations_total{operation="process",outcome="success"} 0
newboot_flow_operations_total{operation="process",outcome="error"} 1
newboot_flow_operations_total{operation="enqueue",outcome="success"} 1
newboot_flow_operations_total{operation="enqueue",outcome="error"} 0
# TYPE newboot_flow_last_success_timestamp_seconds gauge
newboot_flow_last_success_timestamp_seconds{operation="acquire"} 0
newboot_flow_last_success_timestamp_seconds{operation="receive"} 1730000000.25
newboot_flow_last_success_timestamp_seconds{operation="process"} 0
newboot_flow_last_success_timestamp_seconds{operation="enqueue"} 5
# TYPE newboot_flow_last_error_timestamp_seconds gauge
newboot_flow_last_error_timestamp_seconds{operation="acquire"} 0
newboot_flow_last_error_timestamp_seconds{operation="receive"} 0
newboot_flow_last_error_timestamp_seconds{operation="process"} 1730000001.5
newboot_flow_last_error_timestamp_seconds{operation="enqueue"} 0
"""


def series(text: str) -> dict[str, str]:
    """Each sample line of a rendering, as series -> value."""
    samples = [line.rsplit(" ", 1) for line in text.splitlines() if not line.startswith("#")]
    return {name: value for name, value in samples}


def clock(*times: float) -> Iterator[float]:
    return iter(times)


def test_conforms_to_the_platform_vector() -> None:
    contract = json.loads(CONFORMANCE.read_text(encoding="utf-8"))
    times = clock(*(event["timestampSeconds"] for event in contract["events"]))
    flow = Flow(clock=lambda: next(times))
    for event in contract["events"]:
        flow.record(operation=FlowOperation(event["operation"]), success=event["success"])
    rendered = series(flow.render())
    assert len(rendered) == contract["seriesCount"] == 17
    for name, value in contract["expected"].items():
        assert rendered[name] == value, name


def test_renders_like_the_platform_reference() -> None:
    times = clock(1730000000.25, 1730000001.5, 5)
    flow = Flow(clock=lambda: next(times))
    flow.record(operation=FlowOperation.RECEIVE, success=True)
    flow.record(operation=FlowOperation.PROCESS, success=False)
    flow.record(operation=FlowOperation.ENQUEUE, success=True)
    flow.connection(True)
    assert flow.render() == REFERENCE


def test_starts_unknown_and_at_zero() -> None:
    rendered = series(Flow().render())
    assert rendered.pop("newboot_flow_mqtt_connected") == "-1"
    assert set(rendered.values()) == {"0"}
    assert len(rendered) == 16


def test_connection_states() -> None:
    flow = Flow()
    flow.connection(True)
    assert "newboot_flow_mqtt_connected 1\n" in flow.render()
    flow.connection(False)
    assert "newboot_flow_mqtt_connected 0\n" in flow.render()


def test_timestamps_never_go_back() -> None:
    times = clock(10, 4)
    flow = Flow(clock=lambda: next(times))
    flow.record(operation=FlowOperation.ACQUIRE, success=True)
    flow.record(operation=FlowOperation.ACQUIRE, success=True)
    rendered = series(flow.render())
    assert rendered['newboot_flow_last_success_timestamp_seconds{operation="acquire"}'] == "10"
    assert rendered['newboot_flow_operations_total{operation="acquire",outcome="success"}'] == "2"


@pytest.mark.parametrize("now", [float("nan"), float("inf"), -1.0])
def test_refuses_an_invalid_clock(now: float) -> None:
    flow = Flow(clock=lambda: now)
    with pytest.raises(ValueError, match="timestamp"):
        flow.record(operation=FlowOperation.ENQUEUE, success=True)
    assert 'outcome="success"} 1' not in flow.render()


def test_refuses_untyped_arguments() -> None:
    flow = Flow()
    with pytest.raises(TypeError, match="FlowOperation"):
        flow.record(operation="enqueue", success=True)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="FlowOperation"):
        flow.record(operation=FlowOperation.ENQUEUE, success=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="boolean"):
        flow.connection(1)  # type: ignore[arg-type]


def test_counts_from_many_threads() -> None:
    flow = Flow()

    def count() -> None:
        for _ in range(1000):
            flow.record(operation=FlowOperation.RECEIVE, success=True)

    threads = [threading.Thread(target=count) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    total = series(flow.render())[
        'newboot_flow_operations_total{operation="receive",outcome="success"}'
    ]
    assert total == "8000"


def test_content_type_is_prometheus_text() -> None:
    assert METRICS_CONTENT_TYPE == "text/plain; version=0.0.4; charset=utf-8"
