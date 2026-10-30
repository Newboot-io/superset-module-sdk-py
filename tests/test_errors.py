from __future__ import annotations

import superset_module_sdk as sdk


def test_every_public_error_derives_from_sdk_error() -> None:
    for error in (
        sdk.ConfigError,
        sdk.UnsupportedModeError,
        sdk.SecretError,
        sdk.TwinError,
        sdk.TelemetryError,
    ):
        assert issubclass(error, sdk.SdkError)
    assert issubclass(sdk.UnsupportedModeError, sdk.ConfigError)
    assert issubclass(sdk.SdkError, Exception)
