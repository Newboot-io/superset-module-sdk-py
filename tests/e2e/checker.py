"""Drives the running example module over the test broker and asserts what the SDK promises:
telemetry per twin variable, health probes, the flow metrics, the reload command and its failures,
and silence for every command the SDK must not answer. Linked-module input is checked from the
module's log by run.sh: the checker publishes it here."""

import json
import os
import queue
import ssl
import time
import urllib.request

import paho.mqtt.client as mqtt
from fixtures import (
    ARRAY_RELOAD,
    BIG_VARIABLE,
    CLUSTER,
    CUT_RELOAD,
    DEEP_RELOAD,
    DRIP_BODY_RELOAD,
    DRIP_HEADERS_RELOAD,
    FAILING_RELOAD,
    MODULE,
    NODE,
    REDIRECT_RELOAD,
    RELOADED_VARIABLE,
    SHORT_RELOAD,
    STARTUP_VARIABLE,
    STARTUP_VARIABLE_NAME,
    TENANT,
    UPSTREAM_ARRAY_VARIABLE,
    UPSTREAM_HUGE_VARIABLE,
    UPSTREAM_NAN_VARIABLE,
    UPSTREAM_NESTED_ARRAY_VARIABLE,
    UPSTREAM_NESTED_VARIABLE,
    UPSTREAM_RETAINED_VARIABLE,
    UPSTREAM_VARIABLE,
    reloaded_name,
)
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.subscribeoptions import SubscribeOptions

# Above the SDK's 8 KiB message limit. Mosquitto (this harness) honours the largest packet a
# client announces; the superset broker does not and forwards up to 2 MiB, so a module must take
# both sizes without dropping its connection.
BIG_MESSAGE_BYTES = (20 * 1024, 100 * 1024)
COMMAND_PREFIX = f"input/directmethod/{TENANT}/{CLUSTER}/{MODULE}"
COMMAND_TOPIC = f"{COMMAND_PREFIX}/unicast"
INPUT_TOPIC = f"output/telemetry/{TENANT}/{NODE}/sensor/v"
MAX_COMMAND_BYTES = 8192
METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
FLOW_OPERATIONS = ("acquire", "receive", "process", "enqueue")
# The exact series of the common module flow contract, no more, no fewer.
FLOW_SERIES = {
    "newboot_flow_mqtt_connected",
    *(
        f'newboot_flow_operations_total{{operation="{operation}",outcome="{outcome}"}}'
        for operation in FLOW_OPERATIONS
        for outcome in ("success", "error")
    ),
    *(
        f'newboot_flow_{kind}_timestamp_seconds{{operation="{operation}"}}'
        for operation in FLOW_OPERATIONS
        for kind in ("last_success", "last_error")
    ),
}
MAX_SEGMENT_BYTES = 200
MAX_TTL_MS = 30_000
MODULE_HTTP = "http://module:8080"
REPLY_KEYS = 3
REPLY_PREFIX = f"output/directmethod/{TENANT}/{CLUSTER}/{MODULE}/res/"
SCOPE = {"clusterId": CLUSTER, "moduleId": MODULE, "tenantConfigId": TENANT}
SEND_EVERY_S = 5
SILENCE_S = 3
STARTUP_S = 90
TELEMETRY_TOPIC = f"output/telemetry/{TENANT}/{NODE}/{MODULE}/v"
TTL_MS = 5000
UPSTREAM_VALUE = 1.5
WAIT_S = 15

telemetry: "queue.Queue[dict]" = queue.Queue()
replies: "dict[str, queue.Queue[dict]]" = {}


def fail(what):
    print(f"FAIL {what}", flush=True)
    os._exit(1)  # also from paho's thread: a failure never turns into a timeout


def check(condition, what):
    if not condition:
        fail(what)
    print(f"PASS {what}", flush=True)


def reject_constant(name):
    raise ValueError(f"{name} is not JSON")


def on_message(_client, _userdata, message):
    """Collect what the module sends; the checker's own messages to it are skipped."""
    if message.topic != TELEMETRY_TOPIC and not message.topic.startswith(REPLY_PREFIX):
        return
    try:
        body = json.loads(message.payload, parse_constant=reject_constant)
    except ValueError:
        fail(f"non-JSON message on {message.topic}: {message.payload[:80]!r}")
    if message.qos != 0 or message.retain:
        fail(f"{message.topic} sent at QoS {message.qos}, retain {message.retain}")
    if message.topic == TELEMETRY_TOPIC:
        telemetry.put(body)
    else:
        replies.setdefault(message.topic.removeprefix(REPLY_PREFIX), queue.Queue()).put(body)


def valid_segment(value):
    return (
        isinstance(value, str)
        and 0 < len(value.encode()) <= MAX_SEGMENT_BYTES
        and not any(c in "/+#" or ord(c) < 32 or ord(c) == 127 for c in value)
    )


def next_telemetry(deadline_s, variable_id):
    """The next telemetry message for this variable before the deadline."""
    end = time.monotonic() + deadline_s
    while (left := end - time.monotonic()) > 0:
        try:
            message = telemetry.get(timeout=left)
        except queue.Empty:
            break
        if message.get("variableId") == variable_id:
            return message
    return None


def publish(topic, payload, retain=False):
    client.publish(topic, payload, qos=1, retain=retain).wait_for_publish()


def envelope(name, correlation_id, payload, **override):
    now = int(time.time() * 1000)
    fields = {
        "command": name,
        "correlationId": correlation_id,
        "delivery": "unicast",
        "expiresAtUnixMs": now + TTL_MS,
        "issuedAtUnixMs": now,
        "payload": payload,
        "replyTopic": REPLY_PREFIX + correlation_id,
        "target": SCOPE,
    }
    fields.update(override)
    return {key: value for key, value in fields.items() if value is not None}


def command(name, correlation_id, payload, retain=False, delivery="unicast", **override):
    replies.setdefault(correlation_id, queue.Queue())
    body = envelope(name, correlation_id, payload, delivery=delivery, **override)
    publish(f"{COMMAND_PREFIX}/{delivery}/{name}", json.dumps(body), retain)
    return correlation_id


def reply_to(correlation_id, wait_s=WAIT_S):
    """The module's reply, checked against the envelope the cloud accepts."""
    try:
        answer = replies[correlation_id].get(timeout=wait_s)
    except queue.Empty:
        return None
    well_formed = (
        len(answer) == REPLY_KEYS
        and answer.get("correlationId") == correlation_id
        and answer.get("target") == SCOPE
        and (("result" in answer) != ("error" in answer))
        and ("error" not in answer or valid_segment(answer["error"]))
    )
    check(well_formed, f"reply {correlation_id} has exactly correlationId, target, result|error")
    return answer


def http_status(path):
    try:
        with urllib.request.urlopen(MODULE_HTTP + path, timeout=2) as response:
            return response.status
    except OSError as error:
        return getattr(error, "code", None)


def flow_metrics():
    """GET /metrics, checked for the content type: series -> value."""
    with urllib.request.urlopen(MODULE_HTTP + "/metrics", timeout=2) as response:
        check(response.status == 200, "GET /metrics answers 200")
        content_type = response.headers.get("Content-Type")
        check(content_type == METRICS_CONTENT_TYPE, f"/metrics content type: {content_type}")
        text = response.read().decode()
    samples = [line.rsplit(" ", 1) for line in text.splitlines() if not line.startswith("#")]
    metrics = {name: float(value) for name, value in samples}
    check(len(samples) == len(metrics), "/metrics names each series once")
    check(
        set(metrics) == FLOW_SERIES, f"/metrics has exactly the 17 flow series: {sorted(metrics)}"
    )
    return metrics


def operations(metrics, operation, outcome):
    return metrics.get(
        f'newboot_flow_operations_total{{operation="{operation}",outcome="{outcome}"}}'
    )


tls = ssl.create_default_context(cafile="/certs/ca.crt")
tls.load_cert_chain("/certs/checker.pem")
client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id="checker", protocol=mqtt.MQTTv5)
client.tls_set_context(tls)
client.on_message = on_message
client.connect("broker", 8883)
# Retain as published: the checker sees the flag the module set.
client.subscribe([("output/#", SubscribeOptions(qos=1, retainAsPublished=True))])
client.loop_start()

# Sending: one message per variable of the twin fetched at start.
first = next_telemetry(STARTUP_S, STARTUP_VARIABLE)
check(first is not None, "telemetry for the variable listed in the twin at start")
check(first.get("name") == STARTUP_VARIABLE_NAME, "telemetry is named after the variable")
check(first.get("value") is not None, "telemetry has a value")
check(abs(first["timestamp"] - time.time() * 1000) < 60_000, "telemetry timestamp is epoch ms")
check(
    sorted(first) == ["name", "timestamp", "value", "variableId"],
    f"telemetry has exactly name, timestamp, value, variableId: {first}",
)

end = time.monotonic() + STARTUP_S
while http_status("/readyz") != 200 and time.monotonic() < end:
    time.sleep(0.5)
check(http_status("/readyz") == 200, "GET /readyz answers 200 once subscribed")
check(http_status("/healthz") == 200, "GET /healthz answers 200")

# Flow metrics once ready, before any input: connected, the startup telemetry enqueued, nothing
# received yet (commands and acquire are never counted by the SDK).
ready = flow_metrics()
check(ready["newboot_flow_mqtt_connected"] == 1, "newboot_flow_mqtt_connected 1 once ready")
check(operations(ready, "enqueue", "success") > 0, "startup telemetry counted as enqueued")
for operation in ("acquire", "receive", "process"):
    for outcome in ("success", "error"):
        check(operations(ready, operation, outcome) == 0, f"{operation} {outcome} 0 before inputs")

# Receiving: the module logs what on_telemetry delivers; run.sh reads the log. A retained value,
# a NaN, a 1e400, nested values and messages over 8 KiB must never reach the handler.
publish(INPUT_TOPIC, json.dumps({"value": 0, "variableId": UPSTREAM_RETAINED_VARIABLE}), True)
publish(INPUT_TOPIC, b"", retain=True)  # clear it for the next run
publish(INPUT_TOPIC, f'{{"value": NaN, "variableId": "{UPSTREAM_NAN_VARIABLE}"}}')
publish(INPUT_TOPIC, f'{{"value": 1e400, "variableId": "{UPSTREAM_HUGE_VARIABLE}"}}')
publish(INPUT_TOPIC, json.dumps({"value": {"a": [1]}, "variableId": UPSTREAM_NESTED_VARIABLE}))
publish(INPUT_TOPIC, json.dumps({"value": [[1]], "variableId": UPSTREAM_NESTED_ARRAY_VARIABLE}))
publish(INPUT_TOPIC, json.dumps({"value": [1, 2.5, 3.25], "variableId": UPSTREAM_ARRAY_VARIABLE}))
publish(INPUT_TOPIC, json.dumps({"value": UPSTREAM_VALUE, "variableId": UPSTREAM_VARIABLE}))

# Big messages on the input and command topics must not cut the module's connection (run.sh
# checks its log for reconnects): after each size, a reload is still answered.
for size in BIG_MESSAGE_BYTES:
    big = json.dumps({"value": "x" * size, "variableId": BIG_VARIABLE})
    publish(INPUT_TOPIC, big)
    publish(f"{COMMAND_TOPIC}/reload", big)
    answer = reply_to(command("reload", f"after-big-{size}", {}))
    check(
        answer and answer.get("result") == {"reloaded": True},
        f"after {size}-byte messages, reload is still answered",
    )

# Messages that are not command objects must not stop the module.
for raw in ("5", "[1]", "null", "not json", '"text"'):
    publish(f"{COMMAND_TOPIC}/reload", raw)
answer = reply_to(command("reload", "after-junk", {}))
check(answer and answer.get("result") == {"reloaded": True}, f"reload after junk: {answer}")

# Commands this SDK version does not handle get no answer; neither do misaddressed, late,
# retained or oversized reloads.
command("getValue", "get-value", {"variableId": UPSTREAM_VARIABLE})
command("ping", "unknown-command", {})
now = int(time.time() * 1000)
silent = {
    "another-target": {"target": {**SCOPE, "moduleId": "other"}},
    "expired": {"expiresAtUnixMs": now - 1, "issuedAtUnixMs": now - TTL_MS},
    "future-issue": {"expiresAtUnixMs": now + 2 * TTL_MS, "issuedAtUnixMs": now + TTL_MS},
    "long-ttl": {"expiresAtUnixMs": now + MAX_TTL_MS + TTL_MS},
    "no-deadline": {"expiresAtUnixMs": None},
    "text-deadline": {"expiresAtUnixMs": "soon"},
    "wrong-reply-topic": {"replyTopic": REPLY_PREFIX + "elsewhere"},
}
for correlation_id, override in silent.items():
    command("reload", correlation_id, {}, **override)
command("reload", "retained", {}, retain=True)
publish(f"{COMMAND_TOPIC}/reload", b"", retain=True)  # clear it for the next run
replies.setdefault("oversized", queue.Queue())
oversized = envelope("reload", "oversized", {})
oversized["pad"] = "x" * MAX_COMMAND_BYTES
publish(f"{COMMAND_TOPIC}/reload", json.dumps(oversized))
time.sleep(SILENCE_S)
quiet = ["get-value", "unknown-command", *silent, "retained", "oversized", "elsewhere"]
for correlation_id in quiet:
    check(
        replies.get(correlation_id, queue.Queue()).empty(),
        f"no reply to the {correlation_id} command",
    )

# A reload whose payload is no object is refused.
answer = reply_to(command("reload", "reload-bad-payload", "not an object"))
check(answer and answer.get("error") == "invalid_payload", f"invalid payload: {answer}")

# A failed fetch (HTTP 500, an answer cut mid-body, one shorter than announced, a JSON array, a
# redirect, JSON nested too deep) answers a short code and leaves the module working.
failing_reloads = (
    FAILING_RELOAD,
    CUT_RELOAD,
    SHORT_RELOAD,
    ARRAY_RELOAD,
    REDIRECT_RELOAD,
    DEEP_RELOAD,
)
for failing in failing_reloads:
    answer = reply_to(command("reload", failing, {}))
    check(answer and answer.get("error") == "reload_failed", f"failed reload {failing}: {answer}")
# A twin endpoint that answers too slowly: the SDK gives up at its deadline and still answers
# within the command's longest life (30 s).
for slow in (DRIP_HEADERS_RELOAD, DRIP_BODY_RELOAD):
    issued = int(time.time() * 1000)
    longest = {"expiresAtUnixMs": issued + MAX_TTL_MS, "issuedAtUnixMs": issued}
    answer = reply_to(command("reload", slow, {}, **longest), MAX_TTL_MS / 1000)
    check(answer and answer.get("error") == "reload_failed", f"slow reload {slow}: {answer}")

# Then a reload brings the new variables, broadcast this time.
answer = reply_to(command("reload", "reload-1", {}, delivery="broadcast"))
check(answer and answer.get("result") == {"reloaded": True}, f"broadcast reload: {answer}")
# Earlier reloads served the same variable under other names: wait for this reload's name.
end = time.monotonic() + WAIT_S
after = None
while after is None and (left := end - time.monotonic()) > 0:
    after = next_telemetry(left, RELOADED_VARIABLE)
    if after is not None and after.get("name") != reloaded_name("reload-1"):
        after = None
check(after is not None, "after reload, telemetry follows the twin fetched again")
# Drain what was already queued, then the variable the reload dropped is no longer sent.
while not telemetry.empty():
    telemetry.get()
check(
    next_telemetry(2 * SEND_EVERY_S + 1, STARTUP_VARIABLE) is None,
    "a variable dropped from the twin is no longer sent",
)

# Flow metrics after the traffic above: each non-retained input received once and processed once.
# Two are valid (UPSTREAM_VARIABLE and the array); NaN, 1e400, the two nested ones and the two
# over 8 KiB are dropped, so processing errors; the retained one and every command are not counted.
metrics = flow_metrics()
check(metrics["newboot_flow_mqtt_connected"] == 1, "newboot_flow_mqtt_connected 1 at the end")
received = operations(metrics, "receive", "success")
processed = operations(metrics, "process", "success")
dropped = operations(metrics, "process", "error")
check(received == processed + dropped, f"received {received} = processed {processed} + {dropped}")
check((received, processed, dropped) == (8, 2, 6), "8 inputs received, 2 processed, 6 dropped")
check(operations(metrics, "receive", "error") == 0, "no receive errors")
check(operations(metrics, "enqueue", "error") == 0, "no enqueue errors while connected")
check(
    operations(metrics, "enqueue", "success") > operations(ready, "enqueue", "success"), "enqueued"
)
check(
    operations(metrics, "acquire", "success") == operations(metrics, "acquire", "error") == 0,
    "acquire stays at zero",
)
stamp = metrics.get('newboot_flow_last_success_timestamp_seconds{operation="enqueue"}')
check(stamp is not None and abs(stamp - time.time()) < 60, f"last enqueue success is now: {stamp}")

client.loop_stop()
print("ALL CHECKS PASSED", flush=True)
