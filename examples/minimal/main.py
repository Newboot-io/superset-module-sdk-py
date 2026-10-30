"""A minimal Superset module: sends one reading per variable of its module twin every few seconds,
logs its linked inputs by name, follows twin changes, and serves /healthz and /readyz."""

from __future__ import annotations

import logging
import random
import signal
import sys
import threading
import time
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from superset_module_sdk import ModuleClient, ModuleTwin, SdkError, Telemetry

HEALTH_PORT = 8080
RETRY_TWIN_AFTER_S = 3
SEND_EVERY_S = 5

logging.basicConfig(format="%(message)s", level=logging.INFO, stream=sys.stderr)
log = logging.getLogger("module")

try:
    client = ModuleClient.from_env()
    # Stop at start when a declared secret cannot be read. Errors name the secret, never its value.
    client.check_secrets()
except SdkError as error:
    sys.exit(str(error))

variables: Mapping[str, Any] = {}


def follow(twin: ModuleTwin) -> None:
    global variables
    found = twin.get("variables", {})
    variables = found if isinstance(found, dict) else {}
    log.info("twin: %d variables", len(variables))


def load_twin() -> None:
    """At start: keep trying until the module twin arrives. Without it there is nothing to send."""
    while True:
        try:
            follow(client.get_twin())
            return
        except SdkError as error:
            log.error("twin: %s", error)
            time.sleep(RETRY_TWIN_AFTER_S)


def on_input(telemetry: Telemetry) -> None:
    # Names only: a linked module's values are its own business.
    log.info("input: %s (%s)", telemetry.variable_id, telemetry.name)


def read_sensor(_variable_id: str) -> float:
    """Read your machine or sensor here."""
    return round(20 + random.random() * 5, 2)


class Health(BaseHTTPRequestHandler):
    """/healthz: the process answers. /readyz: connected and subscribed."""

    def do_GET(self) -> None:  # noqa: N802 (http.server's name)
        ok = self.path == "/healthz" or (self.path == "/readyz" and client.is_connected())
        self.send_response(200 if ok else 503)
        self.end_headers()

    def log_message(self, *_: Any) -> None:
        pass


def main() -> None:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    server = ThreadingHTTPServer(("", HEALTH_PORT), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client.on_telemetry(on_input)
    client.on_twin_change(follow)
    if client.config.twin is not None:
        threading.Thread(target=load_twin, daemon=True).start()
    else:
        log.info("this module has no module twin: no variables, nothing to send")
    client.connect()
    try:
        while True:
            if client.is_connected():
                for variable_id in list(variables):
                    # The name defaults to the variable's name in the twin.
                    client.send_telemetry(variable_id, read_sensor(variable_id))
            time.sleep(SEND_EVERY_S)
    finally:
        client.close()


if __name__ == "__main__":
    main()
