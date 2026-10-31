# smoother (Python sample)

The middle module of the three-module sample pipeline. It receives `temperature` readings from a
linked module, sends their moving average as `temp_avg`, and that average in Fahrenheit as
`temp_f`.

```
sensor-sim (TypeScript)  --temperature, pressure-->  smoother (Python)  --temp_avg, temp_f-->  alarm-monitor (Rust)
```

- [sensor-sim](https://github.com/Newboot-io/superset-module-sdk-ts/tree/main/samples/sensor-sim)
  sends `temperature` and `pressure`; link its `temperature` to the smoother.
- [smoother](https://github.com/Newboot-io/superset-module-sdk-py/tree/main/samples/smoother)
  (this one) sends `temp_avg` and `temp_f`.
- [alarm-monitor](https://github.com/Newboot-io/superset-module-sdk-rs/tree/main/samples/alarm-monitor)
  receives `temp_avg`; link the smoother's `temp_avg` to it.

It uses only the SDK's public API.

## Twin settings

Read from the module twin at start and again on every reload.

| Field | Type | Default | Rule |
| --- | --- | --- | --- |
| `window` | integer | `5` | Number of samples averaged, 1 to 100. |

A missing or invalid `window` falls back to 5, and the module logs the field name. A smaller
window keeps the most recent samples.

## Variables

The superset assigns variable ids; the smoother picks its variables by their `name`.

| Name | Sent | Value |
| --- | --- | --- |
| `temp_avg` | On each received `temperature` | Moving average of the last `window` temperatures (°C). |
| `temp_f` | On each received `temperature` | `temp_avg` in °F. |

Received messages are matched by their `name` field, so only a linked variable named
`temperature` is used. Non-numeric, boolean or non-finite values are ignored. Each message the
smoother sends is named after its variable in the twin, so the alarm monitor can match `temp_avg`
by name.

## Secrets

None. The module still calls `check_secrets()` at start, as every sample does.

## Logs

Variable ids and message counts only, never values: the twin settings in use, the variable ids it
sends to, and a count of received temperatures (the first, then every 100th).

## Health and metrics

`/healthz` answers 200 while the process runs. `/readyz` answers 200 once the SDK reports
`is_connected()`, meaning connected and subscribed. `/metrics` answers the SDK's flow metrics
(`client.metrics()`, Prometheus text), which the console's data-flow view reads: each message on
its input counts as `receive` and `process`, each sent `temp_avg` and `temp_f` as `enqueue`. All
three are on port 8080.

## Build

From the repository root:

```sh
docker build -f samples/smoother/Dockerfile -t smoother:dev .
```

The image is `linux/amd64` and runs as uid 10001 on a read-only root file system, the way the
Superset runs a module. Built from this repository, the image installs the SDK from this
checkout. In your own project, depend on the published SDK instead:

```sh
pip install "superset-module-sdk @ git+https://github.com/Newboot-io/superset-module-sdk-py@v0.1.0"
```

## Run

**In a Superset cluster:** push the image to a registry the superset can pull from and add it as
a module. Give its twin the variables `temp_avg` and `temp_f`, link the sensor's `temperature`
to the smoother, and link `temp_avg` to the alarm monitor. Optionally set `window` in the twin.

**Locally, against the SDK's end-to-end harness:** `tests/e2e/run.sh` shows how to run a module
the way the Superset does: a throwaway CA, an MQTT broker with per-module access rules, and a
module twin server. To try the smoother there, give the twin server a twin with `temp_avg` and
`temp_f` variables, run this image instead of `examples/minimal`, and publish
`{"name": "temperature", "value": 21.5, "variableId": "..."}` on its input topic.

## Tests

The smoothing logic (`smoothing.py`) is pure and unit-tested with the SDK's tests:

```sh
pip install -e ".[dev]"
pytest samples/smoother/tests
```
