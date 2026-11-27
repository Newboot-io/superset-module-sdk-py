"""Superset module SDK: fetch the module twin, read secrets, send and receive telemetry and image
frames, and answer the superset's reload command, with the flow metrics the Newboot console
reads."""

from .client import ModuleClient
from .config import ModuleConfig, MqttSettings, Target, TwinSettings
from .errors import (
    ConfigError,
    FrameError,
    SdkError,
    SecretError,
    TelemetryError,
    TwinError,
    UnsupportedModeError,
)
from .flow import METRICS_CONTENT_TYPE, Flow, FlowOperation
from .messages import Frame, Telemetry
from .twin import ModuleTwin

__version__ = "0.2.0"

__all__ = [
    "ConfigError",
    "Flow",
    "FlowOperation",
    "Frame",
    "FrameError",
    "METRICS_CONTENT_TYPE",
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
