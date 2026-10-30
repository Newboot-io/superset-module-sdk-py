"""Fetching the module twin: HTTPS with a client certificate, one deadline, a size bound."""

from __future__ import annotations

import http.client
import re
import socket
import ssl
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from ._json import parse_json
from .config import TwinSettings
from .errors import TwinError

CORRELATION_HEADER = "x-newboot-correlation-id"
HTTPS_PORT = 443
MAX_TWIN_BYTES = 2 * 1024 * 1024
TWIN_TIMEOUT_S = 12.0  # within the 15 s the superset gives a reload
_PLAIN_ID = re.compile(r"[A-Za-z0-9-]+")


ModuleTwin = dict[str, Any]
"""This module's twin as received: its fields, plus an optional `variables` object."""


def validate_twin_answer(answer: object, module_id: str) -> ModuleTwin:
    """The answer holds exactly one module, this one; its variables, when present, are an
    object. Returns that module's twin as received."""
    modules = answer.get("modules") if isinstance(answer, dict) else None
    ours = modules.get(module_id) if isinstance(modules, dict) else None
    if not isinstance(modules, dict) or not isinstance(ours, dict) or len(modules) != 1:
        raise TwinError("module twin answer is not this module's")
    if not isinstance(ours.get("variables", {}), dict):
        raise TwinError("module twin variables are not an object")
    return ours


def correlation_headers(correlation_id: str | None) -> dict[str, str]:
    """The correlation header, sent only for a plain id (letters, digits, dashes)."""
    if correlation_id and _PLAIN_ID.fullmatch(correlation_id):
        return {CORRELATION_HEADER: correlation_id}
    return {}


def _resolve(host: str, port: int, addresses: list[tuple[str, int]]) -> None:
    try:
        found = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses.extend((str(info[4][0]), int(info[4][1])) for info in found)
    except OSError:
        pass  # no address: the fetch fails


def fetch_twin(
    settings: TwinSettings,
    module_id: str,
    correlation_id: str | None = None,
    timeout_s: float = TWIN_TIMEOUT_S,
) -> ModuleTwin:
    """GET the twin endpoint. Certificates are read again on every call (they rotate). One
    deadline covers the whole fetch, name lookup to last byte."""
    try:
        return _fetch(settings, module_id, correlation_id, timeout_s)
    except TwinError:
        raise
    except (OSError, ValueError, RecursionError, http.client.HTTPException) as error:
        raise TwinError(f"module twin fetch failed: {type(error).__name__}: {error}") from None
    except Exception as error:  # never a raw library error: the type names it
        raise TwinError(f"module twin fetch failed: {type(error).__name__}") from None


def _fetch(
    settings: TwinSettings, module_id: str, correlation_id: str | None, timeout_s: float
) -> ModuleTwin:
    context = ssl.create_default_context(cafile=settings.ca)
    context.load_cert_chain(settings.identity)  # certificate, chain and key in one file
    endpoint = urlsplit(settings.endpoint)  # keeps ;params in the path
    host = endpoint.hostname or ""
    port = endpoint.port or HTTPS_PORT
    connection = http.client.HTTPSConnection(host, port, context=context)
    held: list[ssl.SSLSocket] = []
    expired = threading.Event()

    def cut() -> None:
        expired.set()
        for sock in held:
            try:
                socket.socket.shutdown(sock, socket.SHUT_RDWR)
            except OSError:
                pass  # already closed

    deadline = time.monotonic() + timeout_s
    watchdog = threading.Timer(timeout_s, cut)
    watchdog.daemon = True
    watchdog.start()
    try:
        # A name lookup cannot be cut: it runs aside, and the fetch stops waiting at the deadline.
        addresses: list[tuple[str, int]] = []
        lookup = threading.Thread(target=_resolve, args=(host, port, addresses), daemon=True)
        lookup.start()
        lookup.join(timeout_s)
        if lookup.is_alive():
            raise TwinError("module twin deadline passed")
        if not addresses:
            raise TwinError("module twin host not found")
        raw: socket.socket | None = None
        failure: OSError | None = None
        for address in addresses:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TwinError("module twin deadline passed")
            try:
                raw = socket.create_connection(address, timeout=left)
                break
            except OSError as error:
                failure = error
        if raw is None:
            raise failure or TwinError("module twin host not reachable")
        sock: ssl.SSLSocket | None = None
        try:
            sock = context.wrap_socket(raw, server_hostname=host, do_handshake_on_connect=False)
            held.append(sock)
            if expired.is_set():  # connected just as time ran out
                raise TwinError("module twin deadline passed")
            sock.do_handshake()
        except BaseException:
            (sock or raw).close()  # the connection does not own it yet
            raise
        connection.sock = sock
        # The endpoint exactly as given: path (with any ;params) and query.
        target = (endpoint.path or "/") + (f"?{endpoint.query}" if endpoint.query else "")
        connection.request("GET", target, headers=correlation_headers(correlation_id))
        response = connection.getresponse()
        body = response.read(MAX_TWIN_BYTES + 1)
        # A body cut short can still end cleanly: compare with what the headers announced.
        if response.length or expired.is_set():
            raise TwinError("module twin answer cut short or too slow")
    finally:
        watchdog.cancel()
        connection.close()
    if response.status != 200 or len(body) > MAX_TWIN_BYTES:
        raise TwinError(f"module twin answer: HTTP {response.status}, {len(body)} bytes")
    return validate_twin_answer(parse_json(body), module_id)
