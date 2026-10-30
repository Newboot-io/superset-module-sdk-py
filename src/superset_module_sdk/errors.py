"""Errors raised by the SDK. Every message names what failed, never a secret value."""


class SdkError(Exception):
    """Base class of every error this SDK raises; raised itself only by connect() after close()."""


class ConfigError(SdkError):
    """The module configuration (module.json) is missing, unreadable or invalid."""


class UnsupportedModeError(ConfigError):
    """The module configuration asks for an MQTT mode this SDK does not support."""


class SecretError(SdkError):
    """A secret is not declared, cannot be read, or is not a JSON string."""


class TwinError(SdkError):
    """The module twin could not be fetched or its answer is not valid."""


class TelemetryError(SdkError):
    """A telemetry message is not valid and was not sent."""
