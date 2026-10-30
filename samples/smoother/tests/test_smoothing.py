from __future__ import annotations

from typing import Any

import pytest
from smoothing import (
    DEFAULT_WINDOW,
    MovingAverage,
    Settings,
    owned_variables,
    read_settings,
    smooth,
    temperature_value,
    to_fahrenheit,
)


@pytest.mark.parametrize("window", [1, 7, 100])
def test_valid_window(window: int) -> None:
    assert read_settings({"window": window}) == Settings(window=window)


@pytest.mark.parametrize(
    "twin",
    [
        {},
        {"window": 0},
        {"window": 101},
        {"window": 2.5},
        {"window": "5"},
        {"window": True},
        {"window": None},
    ],
)
def test_invalid_or_missing_window_falls_back(twin: dict[str, Any]) -> None:
    assert read_settings(twin) == Settings(window=DEFAULT_WINDOW, defaulted=("window",))


def test_owned_variables_are_picked_by_name() -> None:
    twin = {
        "variables": {
            "variable-b": {"name": "temp_f"},
            "variable-a": {"name": "temp_avg"},
            "variable-c": {"name": "temperature"},
            "variable-d": "not an object",
        }
    }
    assert owned_variables(twin) == {"temp_avg": "variable-a", "temp_f": "variable-b"}


def test_first_variable_wins_for_a_repeated_name() -> None:
    twin = {"variables": {"variable-2": {"name": "temp_avg"}, "variable-1": {"name": "temp_avg"}}}
    assert owned_variables(twin) == {"temp_avg": "variable-1"}


@pytest.mark.parametrize("twin", [{}, {"variables": []}, {"variables": {}}])
def test_no_owned_variables(twin: dict[str, Any]) -> None:
    assert owned_variables(twin) == {}


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("temperature", 21.5, 21.5),
        ("temperature", 20, 20.0),
        ("temperature", True, None),
        ("temperature", "21", None),
        ("temperature", [21], None),
        ("temperature", float("nan"), None),
        ("pressure", 1013, None),
        (None, 21.5, None),
    ],
)
def test_temperature_value(name: str | None, value: object, expected: float | None) -> None:
    assert temperature_value(name, value) == expected


def test_to_fahrenheit() -> None:
    assert to_fahrenheit(0) == 32
    assert to_fahrenheit(100) == 212
    assert to_fahrenheit(-40) == -40


def test_moving_average_over_the_window() -> None:
    average = MovingAverage(3)
    assert [average.add(v) for v in (1, 2, 3, 4)] == [1, 1.5, 2, 3]


def test_resize_keeps_the_latest_values() -> None:
    average = MovingAverage(4)
    for value in (1, 2, 3, 4):
        average.add(value)
    average.resize(2)
    assert average.window == 2
    assert average.add(5) == 4.5  # 4 and 5
    average.resize(5)
    assert average.add(6) == 5  # 4, 5 and 6


def test_smooth_rounds_both_values() -> None:
    average = MovingAverage(3)
    smooth(average, 20)
    smooth(average, 21)
    assert smooth(average, 21) == (20.667, 69.2)
