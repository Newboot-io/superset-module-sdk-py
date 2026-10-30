"""Reading the module's secrets: one JSON string per file, read again on every call."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable

from .errors import SecretError

SECRET_SUFFIX = ".json"


def secret_names(secret_files: Iterable[str]) -> list[str]:
    """The names of the declared secrets (their file names without `.json`)."""
    return [
        os.path.basename(path).removesuffix(SECRET_SUFFIX)
        for path in secret_files
        if os.path.basename(path).endswith(SECRET_SUFFIX)
    ]


def read_secret(secret_files: Iterable[str], name: str) -> str:
    """The value of secret `name`. Errors name the secret only, never its value."""
    for path in secret_files:
        if os.path.basename(path) == f"{name}{SECRET_SUFFIX}":
            try:
                with open(path, encoding="utf-8") as file:
                    value = json.load(file)
            except (OSError, ValueError, RecursionError):
                # Never pass the parse error on: it can quote the file.
                raise SecretError(f"secret {name} cannot be read") from None
            if not isinstance(value, str):
                raise SecretError(f"secret {name} is not a string")
            return value
    raise SecretError(f"secret {name} is not declared")
