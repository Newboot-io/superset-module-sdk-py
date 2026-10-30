"""The module's MQTT topics, built from its target."""

from __future__ import annotations

from .config import Target


def telemetry_topic(target: Target) -> str:
    """Where the module publishes its own telemetry."""
    return f"output/telemetry/{target.tenant_config_id}/{target.node_id}/{target.module_id}/v"


def command_topic(target: Target) -> str:
    """The prefix of the module's commands: `<prefix>/<delivery>/<command>`."""
    return f"input/directmethod/{target.tenant_config_id}/{target.cluster_id}/{target.module_id}"


def reply_topic(target: Target) -> str:
    """The prefix of the module's replies: `<prefix>/<correlationId>`."""
    return (
        f"output/directmethod/{target.tenant_config_id}/{target.cluster_id}/{target.module_id}/res"
    )


def command_subscription(target: Target) -> str:
    """The filter that matches every command addressed to the module."""
    return f"{command_topic(target)}/+/+"
