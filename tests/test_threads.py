from __future__ import annotations

import threading

import pytest

from superset_module_sdk._json import parse_json
from superset_module_sdk._threads import call_with_deep_stack, start_thread


def test_call_with_deep_stack_returns_and_restores_the_default() -> None:
    assert call_with_deep_stack(lambda: 42) == 42
    assert threading.stack_size() == 0


def test_call_with_deep_stack_raises_the_functions_error() -> None:
    def broken() -> int:
        raise KeyError("x")

    with pytest.raises(KeyError):
        call_with_deep_stack(broken)


def test_deep_json_raises_recursion_error_not_a_crash() -> None:
    with pytest.raises(RecursionError):
        call_with_deep_stack(lambda: parse_json(b"[" * 100_000 + b"]" * 100_000))


def test_start_thread_runs_the_target() -> None:
    done = threading.Event()
    start_thread(done.set)
    assert done.wait(5)
