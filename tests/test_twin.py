from __future__ import annotations

from typing import Any

import pytest

from superset_module_sdk import TwinError
from superset_module_sdk.twin import correlation_headers, validate_twin_answer

MODULE = "my-module"


def test_valid_answer_returns_the_twin_as_received() -> None:
    answer = {
        "modules": {MODULE: {"serverport": 4840, "variables": {"v": {"name": "Boiler"}}}},
        "path": "/site/scope",
    }
    twin = validate_twin_answer(answer, MODULE)
    assert twin == {"serverport": 4840, "variables": {"v": {"name": "Boiler"}}}


def test_variables_are_optional() -> None:
    assert validate_twin_answer({"modules": {MODULE: {"a": 1}}}, MODULE) == {"a": 1}


@pytest.mark.parametrize(
    "answer",
    [
        [1],
        {},
        {"modules": []},
        {"modules": {"other": {}}},
        {"modules": {MODULE: {}, "other": {}}},
        {"modules": {MODULE: "text"}},
        {"modules": {MODULE: {"variables": [1]}}},
    ],
)
def test_invalid_answers(answer: Any) -> None:
    with pytest.raises(TwinError):
        validate_twin_answer(answer, MODULE)


def test_correlation_header_only_for_plain_ids() -> None:
    assert correlation_headers("reload-1") == {"x-newboot-correlation-id": "reload-1"}
    assert correlation_headers(None) == {}
    assert correlation_headers("") == {}
    assert correlation_headers("a b\r\nx: y") == {}
