from __future__ import annotations

import traceback
from pathlib import Path

import pytest

from superset_module_sdk import SecretError
from superset_module_sdk._secret import read_secret, secret_names

VALUE = "s3cr3t-value"


def write(tmp_path: Path, content: str) -> list[str]:
    path = tmp_path / "device-password.json"
    path.write_text(content)
    return [str(path)]


def test_reads_a_json_string(tmp_path: Path) -> None:
    files = write(tmp_path, f'"{VALUE}"')
    assert read_secret(files, "device-password") == VALUE


def test_reads_again_on_every_call(tmp_path: Path) -> None:
    files = write(tmp_path, '"first"')
    assert read_secret(files, "device-password") == "first"
    write(tmp_path, '"second"')
    assert read_secret(files, "device-password") == "second"


@pytest.mark.parametrize(
    ("content", "message"),
    [(VALUE, "cannot be read"), (f'{{"value": "{VALUE}"}}', "is not a string")],
)
def test_errors_never_quote_the_value(tmp_path: Path, content: str, message: str) -> None:
    files = write(tmp_path, content)
    with pytest.raises(SecretError) as caught:
        read_secret(files, "device-password")
    assert str(caught.value) == f"secret device-password {message}"
    assert VALUE not in repr(caught.value)
    # Nor does the traceback, chained parse errors included.
    assert VALUE not in "".join(traceback.format_exception(caught.value))


def test_undeclared_and_missing(tmp_path: Path) -> None:
    with pytest.raises(SecretError, match="secret other is not declared"):
        read_secret([str(tmp_path / "device-password.json")], "other")
    with pytest.raises(SecretError, match="cannot be read"):
        read_secret([str(tmp_path / "device-password.json")], "device-password")


def test_secret_names() -> None:
    assert secret_names(["/v/a.json", "/v/b-c.json", "/v/notes.txt"]) == ["a", "b-c"]


def test_deeply_nested_file_names_the_secret_only(tmp_path: Path) -> None:
    files = write(tmp_path, "[" * 100_000 + "]" * 100_000)
    with pytest.raises(SecretError, match="^secret device-password cannot be read$"):
        read_secret(files, "device-password")
