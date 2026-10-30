"""smoother: receives `temperature` from a linked module and sends its moving average (`temp_avg`)
and that average in Fahrenheit (`temp_f`). The window comes from the module twin."""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from smoothing import (
    AVERAGE_NAME,
    FAHRENHEIT_NAME,
    MovingAverage,
    owned_variables,
    read_settings,
    smooth,
    temperature_value,
)

from superset_module_sdk import ModuleClient, ModuleTwin, SdkError, Telemetry

HEALTH_PORT = 8080
LOG_EVERY = 100
RETRY_TWIN_AFTER_S = 3

logging.basicConfig(format="%(message)s", level=logging.INFO, stream=sys.stderr)
log = logging.getLogger("smoother")


class Smoother:
    """The module's state: the moving average and the ids of the variables it sends."""

    def __init__(self, client: ModuleClient) -> None:
        self.client = client
        self.average = MovingAverage()
        self.variables: dict[str, str] = {}
        self.received = 0
        self.lock = threading.Lock()

    def apply(self, twin: ModuleTwin) -> None:
        """Called with the twin at start and again on every reload."""
        settings = read_settings(twin)
        for field in settings.defaulted:
            log.warning("twin: %s missing or invalid, using the default", field)
        variables = owned_variables(twin)
        with self.lock:
            self.average.resize(settings.window)
            self.variables = variables
        log.info("twin: window %d, sending %s", settings.window, sorted(variables.values()))

    def on_telemetry(self, telemetry: Telemetry) -> None:
        celsius = temperature_value(telemetry.name, telemetry.value)
        if celsius is None:
            return
        with self.lock:
            average, fahrenheit = smooth(self.average, celsius)
            variables = dict(self.variables)
            self.received += 1
            received = self.received
        # The SDK names each message after its variable in the twin.
        for name, value in ((AVERAGE_NAME, average), (FAHRENHEIT_NAME, fahrenheit)):
            if name in variables:
                self.client.send_telemetry(variables[name], value)
        if received == 1 or received % LOG_EVERY == 0:
            log.info("received %d temperature messages from %s", received, telemetry.variable_id)


def main() -> None:
    try:
        client = ModuleClient.from_env()
        client.check_secrets()  # the smoother declares none; a module that does stops here
    except SdkError as error:
        sys.exit(str(error))
    smoother = Smoother(client)

    class Health(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server's name)
            ok = self.path == "/healthz" or (self.path == "/readyz" and client.is_connected())
            self.send_response(200 if ok else 503)
            self.end_headers()

        def log_message(self, *_: Any) -> None:
            pass

    def load_twin() -> None:
        while True:
            try:
                smoother.apply(client.get_twin())
                return
            except SdkError as error:
                log.error("twin: %s", error)
                time.sleep(RETRY_TWIN_AFTER_S)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    server = ThreadingHTTPServer(("", HEALTH_PORT), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client.on_twin_change(smoother.apply)
    client.on_telemetry(smoother.on_telemetry)
    if client.config.twin is not None:
        threading.Thread(target=load_twin, daemon=True).start()
    else:
        log.info("no module twin: default window, no variables to send")
    client.connect()
    try:
        while True:
            time.sleep(60)
    finally:
        client.close()


if __name__ == "__main__":
    main()
