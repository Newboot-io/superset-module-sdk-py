"""Newboot's common module flow metrics: 17 Prometheus series the console reads to show a module's
activity; the rendered text is the reference's, byte for byte.

Ported from Newboot's reference implementation of the module flow-metrics contract and contributed
by Newboot under Apache-2.0; conformance vector at revision 589be4b6, identical in every SDK."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from math import isfinite

CONNECTED = "newboot_flow_mqtt_connected"
LAST_ERROR = "newboot_flow_last_error_timestamp_seconds"
LAST_SUCCESS = "newboot_flow_last_success_timestamp_seconds"
OPERATIONS = "newboot_flow_operations_total"
UNKNOWN_CONNECTION = -1
# What a /metrics answer carries: the Prometheus text format.
METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


class FlowOperation(str, Enum):
    """The fixed set of operations a module counts."""

    ACQUIRE = "acquire"
    RECEIVE = "receive"
    PROCESS = "process"
    ENQUEUE = "enqueue"


@dataclass
class _Counters:
    error: int = 0
    last_error: float = 0.0
    last_success: float = 0.0
    success: int = 0


class Flow:
    """Counts operations and the MQTT connection state; safe to use from any thread."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._connected = UNKNOWN_CONNECTION
        self._lock = threading.Lock()
        self._operations = {operation: _Counters() for operation in FlowOperation}

    def connection(self, connected: bool) -> None:
        """Record the MQTT connection state: 1 connected, 0 not (-1 until first recorded)."""
        if not isinstance(connected, bool):
            raise TypeError("connected must be boolean")
        with self._lock:
            self._connected = int(connected)

    def record(self, *, operation: FlowOperation, success: bool) -> None:
        """Count one completed operation and stamp its last success or error time."""
        if not isinstance(operation, FlowOperation) or not isinstance(success, bool):
            raise TypeError("record requires a FlowOperation and boolean success")
        now = self._clock()
        if not isfinite(now) or now < 0:
            raise ValueError("invalid flow timestamp")
        with self._lock:
            values = self._operations[operation]
            if success:
                values.success += 1
                values.last_success = max(values.last_success, now)
            else:
                values.error += 1
                values.last_error = max(values.last_error, now)

    def render(self) -> str:
        """The 17 series in the Prometheus text format."""
        with self._lock:
            lines = [f"# TYPE {CONNECTED} gauge", f"{CONNECTED} {self._connected}"]
            kinds = ((OPERATIONS, "counter"), (LAST_SUCCESS, "gauge"), (LAST_ERROR, "gauge"))
            for metric, kind in kinds:
                lines.append(f"# TYPE {metric} {kind}")
                for operation, values in self._operations.items():
                    name = operation.value
                    if metric == OPERATIONS:
                        for outcome in ("success", "error"):
                            count = getattr(values, outcome)
                            lines.append(
                                f'{metric}{{operation="{name}",outcome="{outcome}"}} {count}'
                            )
                    else:
                        stamp = values.last_success if metric == LAST_SUCCESS else values.last_error
                        # .17g as the reference: 3 for 3.0, which parses to the same float.
                        lines.append(f'{metric}{{operation="{name}"}} {stamp:.17g}')
            return "\n".join(lines) + "\n"
