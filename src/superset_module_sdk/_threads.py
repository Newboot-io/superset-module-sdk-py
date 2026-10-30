"""Threads with a stack deep enough to parse nested JSON: a thread's default stack is small on
some systems (musl), and the JSON parser recurses."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TypeVar

THREAD_STACK_BYTES = 8 * 1024 * 1024
_stack_lock = threading.Lock()

T = TypeVar("T")


@contextmanager
def deep_stack() -> Iterator[None]:
    """Threads started inside this block get THREAD_STACK_BYTES of stack."""
    with _stack_lock:
        previous = threading.stack_size(THREAD_STACK_BYTES)
        try:
            yield
        finally:
            threading.stack_size(previous)


def start_thread(target: Callable[[], None]) -> None:
    """Start a daemon thread with a deep stack."""
    with deep_stack():
        threading.Thread(target=target, daemon=True).start()


def call_with_deep_stack(function: Callable[[], T]) -> T:
    """Run `function` on a deep-stack thread, wait for it, and return its result or raise its
    exception: safe from any caller's thread, whatever its stack size."""
    results: list[T] = []
    failures: list[BaseException] = []

    def run() -> None:
        try:
            results.append(function())
        except BaseException as error:  # handed back to the caller
            failures.append(error)

    with deep_stack():
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
    thread.join()
    if failures:
        raise failures[0]
    return results[0]
