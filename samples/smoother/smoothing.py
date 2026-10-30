"""The smoother's own logic, free of I/O: twin settings, variable choice and the moving average."""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_WINDOW = 5
MAX_WINDOW = 100
MIN_WINDOW = 1
INPUT_NAME = "temperature"
AVERAGE_NAME = "temp_avg"
FAHRENHEIT_NAME = "temp_f"
OWNED_NAMES = (AVERAGE_NAME, FAHRENHEIT_NAME)
DECIMALS = 3


@dataclass(frozen=True)
class Settings:
    """The smoother's twin settings, and the fields that fell back to their default."""

    window: int
    defaulted: tuple[str, ...] = ()


def read_settings(twin: Mapping[str, Any]) -> Settings:
    """`window`: an integer from 1 to 100; missing or invalid falls back to 5."""
    window = twin.get("window")
    valid = (
        isinstance(window, int)
        and not isinstance(window, bool)
        and MIN_WINDOW <= window <= MAX_WINDOW
    )
    if valid:
        assert isinstance(window, int)
        return Settings(window=window)
    return Settings(window=DEFAULT_WINDOW, defaulted=("window",))


def owned_variables(twin: Mapping[str, Any]) -> dict[str, str]:
    """The ids of the variables this module sends, by name: `temp_avg` and `temp_f` when the twin
    lists them. The superset assigns the ids; the names identify them."""
    variables = twin.get("variables")
    if not isinstance(variables, dict):
        return {}
    found: dict[str, str] = {}
    for variable_id, variable in sorted(variables.items()):
        name = variable.get("name") if isinstance(variable, dict) else None
        if name in OWNED_NAMES and name not in found:
            found[name] = variable_id
    return found


def temperature_value(name: str | None, value: object) -> float | None:
    """A received value the smoother uses: a finite number sent under the name `temperature`."""
    if name != INPUT_NAME or isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def to_fahrenheit(celsius: float) -> float:
    return celsius * 9 / 5 + 32


class MovingAverage:
    """The average of the last `window` values."""

    def __init__(self, window: int = DEFAULT_WINDOW) -> None:
        self._values: deque[float] = deque(maxlen=window)

    @property
    def window(self) -> int:
        maxlen = self._values.maxlen
        assert maxlen is not None
        return maxlen

    def resize(self, window: int) -> None:
        """Change the window, keeping the most recent values that still fit."""
        if window != self.window:
            self._values = deque(self._values, maxlen=window)

    def add(self, value: float) -> float:
        self._values.append(value)
        return sum(self._values) / len(self._values)


def smooth(average: MovingAverage, celsius: float) -> tuple[float, float]:
    """Add one temperature; returns (temp_avg, temp_f), rounded to 3 decimals."""
    mean = average.add(celsius)
    return round(mean, DECIMALS), round(to_fahrenheit(mean), DECIMALS)
