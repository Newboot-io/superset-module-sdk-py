"""Superset module SDK: fetch the module twin, read secrets, send and receive telemetry, and
answer the superset's reload command."""

from .client import ModuleClient
from .config import ModuleConfig, MqttSettings, Target, TwinSettings
from .errors import (
    ConfigError,
    SdkError,
    SecretError,
    TelemetryError,
    TwinError,
    UnsupportedModeError,
)
from .messages import Telemetry
from .twin import ModuleTwin

__version__ = "0.1.0"

__all__ = [
    "ConfigError",
    "ModuleClient",
    "ModuleConfig",
    "SdkError",
    "ModuleTwin",
    "MqttSettings",
    "SecretError",
    "Target",
    "Telemetry",
    "TelemetryError",
    "TwinError",
    "TwinSettings",
    "UnsupportedModeError",
    "__version__",
]
