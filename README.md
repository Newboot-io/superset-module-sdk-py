# superset-module-sdk (Python)

A small library for writing modules that run in a Newboot Superset. It handles the wire contract
for you, so a module can:

- fetch its **module twin** (the fields saved in the console and its variables),
- read its **secrets**,
- **send telemetry** on its own topic,
- **receive telemetry** from the modules the Superset links to it,
- follow **twin changes**: the built-in answer to the Superset's `reload` command.

Sister libraries with the same API shape and the same wire behaviour:
[superset-module-sdk-ts](https://github.com/Newboot-io/superset-module-sdk-ts) (TypeScript) and
[superset-module-sdk-rs](https://github.com/Newboot-io/superset-module-sdk-rs) (Rust).

## Requirements

- Python 3.10 or later
- [`paho-mqtt`](https://pypi.org/project/paho-mqtt/) 2.x (installed as a dependency)
- A module deployed by a Superset whose broker connection uses `mutual-tls` (see
  [Compatibility](#compatibility))

## Install

The package is installed from GitHub for now; package registries come later.

```sh
pip install "superset-module-sdk @ git+https://github.com/Newboot-io/superset-module-sdk-py@v0.1.0"
```

Import it as `superset_module_sdk`.

## Quick start

```python
import time

from superset_module_sdk import ModuleClient, ModuleTwin, Telemetry, TelemetryError

client = ModuleClient.from_env()            # reads /etc/newboot/module.json
client.check_secrets()                      # stop early if a declared secret is unreadable
for name in client.secret_names():
    secret = client.get_secret(name)        # use it; never log it

twin = client.get_twin()                    # this module's twin, as a dict
variables = dict(twin.get("variables", {}))


def follow(new_twin: ModuleTwin) -> None:   # called on every successful reload
    variables.clear()
    variables.update(new_twin.get("variables", {}))


def on_input(message: Telemetry) -> None:   # a linked module's value
    print("input:", message.variable_id)


client.on_twin_change(follow)
client.on_telemetry(on_input)
client.connect()                            # background MQTT connection

while True:
    if client.is_connected():
        for variable_id in list(variables):
            try:
                # The name defaults to the variable's name in the twin.
                client.send_telemetry(variable_id, 21.5)
            except TelemetryError as error:  # names the field, never the value
                print("telemetry:", error)
    time.sleep(5)
```

A complete module, with `/healthz` and `/readyz` on port 8080, a retry loop for the twin at start
and a Dockerfile, is in [`examples/minimal`](examples/minimal).

[`samples/smoother`](samples/smoother) is a module of the three-language sample pipeline: it
averages the `temperature` a linked module sends and passes `temp_avg` and `temp_f` on.

## API reference

Errors, all exported from `superset_module_sdk`:

| Error | Raised when |
| --- | --- |
| `SdkError` | Base class of every error below; raised itself only by `connect()` after `close()`. |
| `ConfigError` | `module.json` is missing, not JSON, or invalid. |
| `UnsupportedModeError` | Subclass of `ConfigError`: `mqtt.mode` is not `mutual-tls`. |
| `SecretError` | A secret is not declared, cannot be read, or is not a string. |
| `TwinError` | No twin is configured, or a twin fetch or its answer fails. |
| `TelemetryError` | A telemetry message breaks the payload rules. |

No other exception escapes the public API (catch `SdkError` for all of them). No error message ever
contains a secret's value or the content of a secret file.

### `ModuleClient.from_env() -> ModuleClient`

Reads the module configuration from the file named by the `MODULE_CONFIG` environment variable
(default `/etc/newboot/module.json`) and validates it:

- `mqtt.mode`, checked first: anything but `mutual-tls` raises `UnsupportedModeError`, whatever
  the rest holds;
- `brokerUrl`: exactly `scheme://host[:port]`, scheme `tcps` or `mqtts`; host a DNS name
  (`[A-Za-z0-9.-]+`) or a bracketed IPv6 literal; port 1 to 5 digits, no sign, 1-65535, 8883 when
  absent (`host:` with an empty port is refused); no user information, path, query or fragment.
  The rule is matched on the raw string: surrounding spaces, tabs or newlines are refused, never
  trimmed;
- `mqtt.identity` and `mqtt.ca`: non-empty paths;
- `target`: `clusterId`, `moduleId`, `nodeId`, `tenantConfigId`, each a valid topic level (no
  control character, `/`, `+` or `#`);
- `twin` (optional; `null` is the same as absent): `endpoint` (an `https` URL), `identity` and `ca`
  non-empty paths;
- `configuration.inputs`: topics, without `+`, `#` or control characters;
- `configuration.secretFiles`: non-empty paths, each ending in `.json`.

- **Raises** `UnsupportedModeError` for any other `mqtt.mode`, `ConfigError` when the file is
  missing, is not JSON, or is otherwise invalid.
- `ModuleClient(config)` takes an already parsed `ModuleConfig` (`ModuleConfig.parse(dict)`,
  `ModuleConfig.from_file(path)`). `client.config` exposes it, including `configuration` (your
  module's own settings) and `target`.

### `get_twin(correlation_id: str | None = None) -> ModuleTwin`

`GET` on the twin endpoint over HTTPS, with the twin client certificate (one PEM file holding the
certificate, its chain and the key) and the twin CA. The files are read again on every call, so
rotated certificates are picked up.

- One 12 s deadline covers the whole fetch, name lookup to last byte; answers are bounded to 2 MiB;
  only `200` is accepted (no redirects); a body shorter than its `content-length` is refused.
- The endpoint is requested exactly as configured: its path (with any `;params`) and query.
- The answer must hold exactly one module, this one, its `variables` must be an object, and every
  number in it must be finite as an IEEE-754 double.
- Any failure while building the request, such as an unreadable or corrupt certificate, is a
  `TwinError`.
- `correlation_id`, when made of letters, digits and dashes only, is sent as the
  `x-newboot-correlation-id` header.
- **Returns** this module's twin exactly as received, a `dict` (`ModuleTwin`): its fields, plus an
  optional `variables` object mapping each variable id to its twin (`name`, `modelId`, ...). The
  client keeps the last twin it fetched to name telemetry (see `send_telemetry`).
- **Raises** `TwinError` when the module has no twin configured, for any failed fetch or invalid
  answer, or for a correlation id that is not a string. There is no retry: call again later.

### `get_secret(name: str) -> str`

Finds `<name>.json` among `configuration.secretFiles`, reads it (on every call: a new value replaces
the file while the module runs) and returns the JSON string it holds.

- **Raises** `SecretError` with one of: `secret <name> is not declared`,
  `secret <name> cannot be read` (missing file, invalid or too deeply nested JSON),
  `secret <name> is not a string`.
### `secret_names() -> list[str]` and `check_secrets() -> None`

`secret_names()` lists the declared secrets. `check_secrets()` reads every one of them and raises
`SecretError` naming the first that cannot be read: call it at start to stop early.

### `connect() -> None`

Starts the MQTT 5 connection over TLS to `brokerUrl` in a background thread and returns at once.

- It never fails for missing or unreadable certificate files: it logs
  `mqtt: certificates cannot be loaded: <error type>` and tries again, reading the files again.
  Only the module's CA (`mqtt.ca`) is trusted, never the system's.
- A second `connect()` does nothing; `connect()` after `close()` raises `SdkError`.
- Client id `<moduleId>-<hostname>`. When a connection attempt fails or the connection drops, it
  tries again about every 5 s (the same in the three SDKs); the first retry after a failed start can
  take up to 10 s. It reads the client certificate and CA again before each attempt.
- Connection problems are logged, never with certificate contents: `mqtt: connection failed`,
  `mqtt: connection refused: <reason code>`, `mqtt: subscription refused`,
  `mqtt: disconnected, reconnecting`.
- The CONNECT packet announces an MQTT 5 maximum packet size of 2 MiB + 1 KiB (2098176 bytes); see
  [Security notes](#security-notes) for what that does and does not protect.
- On every connection it subscribes, QoS 1, retain-as-published, without stored messages, to the
  module's command topic (`input/directmethod/<tenant>/<cluster>/<module>/+/+`) and to each topic in
  `configuration.inputs`.
- `is_connected() -> bool` is true when the client is connected **and** subscribed: it is what your
  module's `/readyz` should answer.

### `send_telemetry(variable_id, value, name=None, timestamp=None) -> bool`

Publishes `{"variableId", "value", "timestamp", "name"}` as JSON on
`output/telemetry/<tenant>/<node>/<module>/v`, QoS 0, not retained. The payload rules, also applied
to received telemetry:

- `variable_id`: a key of `variables` in the module twin (non-empty, at most 200 bytes, no `/`,
  `+`, `#` or control character).
- `value`: a number finite as an IEEE-754 double, a string, a boolean, or an array of those.
  Objects, nested arrays, `None`, NaN, infinity and integers beyond the double range are refused.
- `timestamp`: epoch milliseconds, an integer from 0 to 2^53-1; a float with an integral value
  (`1.7e12`) counts and is sent as an integer. Defaults to now.
- `name`: a string; when not given, the variable's `name` in the last fetched twin, else left out.
- **Returns** `True` when handed to the connected client, `False` when the client is not connected
  and subscribed, including before `connect()`. Nothing is queued while disconnected.
- **Raises** `TelemetryError` naming the invalid field (`telemetry value is not valid`), including
  for a `variable_id` that is not a string.

### `on_telemetry(handler: Callable[[Telemetry], None]) -> None`

Adds a handler for messages on the linked modules' topics (`configuration.inputs`); several
handlers run in registration order. `Telemetry` has `topic`, `variable_id`, `value`, `name`
(or `None` when absent) and `timestamp` (or `None` when absent). The SDK drops, before any handler
sees them: retained messages, messages over 8 KiB, payloads that are not JSON objects, messages
with a non-finite number anywhere (NaN, infinity, beyond the double range), and messages that break
the payload rules of `send_telemetry` (for example nested values, or `"name": null` and
`"timestamp": null`: optional fields may be absent, not null).
Handlers run on the MQTT thread; an exception in one is logged by type only (never with payload
values) and does not stop the others.

### `on_twin_change(handler: Callable[[ModuleTwin], None]) -> None`

Adds a handler (several run in registration order) called with the freshly fetched twin when the
Superset sends `reload`. A handler that raises is logged by error type only, the handlers after it
are skipped, and the reload answers `reload_failed`.

### `close() -> None`

Disconnects and stops the background thread. The client cannot connect again.

## Reload behaviour

When the twin or its linked variables change in the console, the Superset sends the `reload`
command to the module. The SDK answers it for you:

1. The command is accepted only when it is addressed to this module (topic, `delivery`
   `unicast`/`broadcast`, `replyTopic`, exact `target`), not retained, at most 8 KiB, and on time
   (issued no later than now + 1 s, not expired, valid at most 30 s). Otherwise it gets no answer.
2. A `payload` that is not an object is answered `{"error": "invalid_payload"}`.
3. Otherwise the SDK fetches the twin again (one reload at a time, with the command's correlation
   id), calls every `on_twin_change` handler, and answers `{"result": {"reloaded": true}}`. If the
   fetch or a handler fails, it answers `{"error": "reload_failed"}`.
4. Replies go to `output/directmethod/<tenant>/<cluster>/<module>/res/<correlationId>`, QoS 0, and
   hold exactly `correlationId`, `target` and `result` or `error`. A reply is sent only while the
   client is connected and subscribed, and never after the command expired; it is never queued.

Other commands (`getValue`, custom commands) are not handled in this version: the SDK ignores them
and sends no answer.

## Security notes

- Certificates rotate: the MQTT client certificate and CA are re-read before every (re)connection,
  the twin certificate and CA on every `get_twin()` call. A rotated CA is added; the previous one
  stays trusted until the module restarts.
- Secrets are read from their file on every call and are never logged. Error messages name the
  secret only, never its value, and never a JSON parse error (which could quote the file).
- What the module may publish and read is enforced by the Superset broker's per-module access rules,
  which the Superset generates; the SDK only uses the module's own topics and its declared inputs.
- The client announces a 2 MiB + 1 KiB maximum packet size to the broker on CONNECT. paho enforces
  it only after a packet has been read, so the SDK relies on the broker (trusted, reached over mutual
  TLS) honouring it. Messages over 8 KiB are then dropped before parsing.
- Errors from your handlers are logged by type only: their messages may quote values.
- Run your module the way the Superset does: as uid 10001, read-only root file system, no
  capabilities (see the example Dockerfile).

## Compatibility

- Broker connection mode `mutual-tls` only. `from_env()` refuses any other `mqtt.mode` with
  `UnsupportedModeError`.
- Python 3.10 or later (CI runs 3.10, 3.12 and 3.13), `paho-mqtt` 2.x, `linux/amd64` images.

## Testing

Unit tests (no network, at least 90% line coverage enforced), type check and lint:

```sh
pip install -e ".[dev]"
pytest
mypy
ruff check . && ruff format --check .
```

The end-to-end test builds `examples/minimal`, starts a throwaway CA, an MQTT broker with a
default-deny access list and a module twin server on an internal Docker network, runs the module
the way the Superset does (uid 10001, read-only root, no capabilities) and checks telemetry,
health endpoints, reload answers and failures, ignored commands, dropped inputs and secret
handling. It needs Docker only, and removes everything it creates:

```sh
tests/e2e/run.sh
```

## Contributing

Issues and pull requests are welcome. Keep changes small, with unit tests for every new rule, and
run the commands of [Testing](#testing) before opening a pull request. Behaviour on the wire must
stay identical across the Python, TypeScript and Rust SDKs.

## License

Apache License 2.0, see [LICENSE](LICENSE).
