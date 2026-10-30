"""fetch_twin against a local HTTPS server that requires a client certificate."""

from __future__ import annotations

import json
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import Certs

from superset_module_sdk import TwinError, TwinSettings
from superset_module_sdk.twin import MAX_TWIN_BYTES, fetch_twin

MODULE = "my-module"
GOOD = {"modules": {MODULE: {"serverport": 4840, "variables": {"v": {"name": "n"}}}}}


class Handler(BaseHTTPRequestHandler):
    seen_headers: list[dict[str, str]] = []
    seen_paths: list[str] = []

    def answer(self, status: int, body: bytes, length: int | None = None) -> None:
        self.send_response(status)
        self.send_header("content-length", str(len(body) if length is None else length))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        Handler.seen_headers.append(dict(self.headers))
        Handler.seen_paths.append(self.path)
        if self.path in ("/twin", "/twin?module=my-module&x=1", "/twin;v=1?x=1"):
            self.answer(200, json.dumps(GOOD).encode())
        elif self.path == "/error":
            self.answer(500, b"")
        elif self.path == "/cut":
            self.answer(200, b"{" * 10, length=1000)
        elif self.path == "/big":
            self.answer(200, b" " * (MAX_TWIN_BYTES + 1))
        elif self.path == "/slow":
            time.sleep(1.5)
            self.answer(200, json.dumps(GOOD).encode())
        elif self.path == "/other":
            self.answer(200, json.dumps({"modules": {"other": {}}}).encode())
        elif self.path == "/huge-int":
            self.answer(200, b'{"modules": {"my-module": {"x": 1' + b"0" * 400 + b"}}}")
        elif self.path == "/nan":
            self.answer(200, b'{"modules": {"my-module": {"x": NaN}}}')

    def log_message(self, *_: object) -> None:
        pass


@pytest.fixture(scope="module")
def server(certs: Certs) -> Iterator[int]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(certs.ca)
    context.load_cert_chain(certs.server_cert, certs.server_key)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()


def settings(certs: Certs, port: int, path: str) -> TwinSettings:
    return TwinSettings(
        ca=certs.ca, endpoint=f"https://localhost:{port}{path}", identity=certs.client
    )


def test_fetch_twin_with_correlation_id(certs: Certs, server: int) -> None:
    twin = fetch_twin(settings(certs, server, "/twin"), MODULE, "reload-7")
    assert twin == GOOD["modules"][MODULE]
    assert Handler.seen_headers[-1].get("x-newboot-correlation-id") == "reload-7"


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("/error", "HTTP 500"),
        ("/cut", "cut short"),
        ("/big", "bytes|cut short"),
        ("/other", "not this module's"),
        ("/nan", "fetch failed"),
        ("/huge-int", "fetch failed"),
    ],
)
def test_fetch_twin_failures(certs: Certs, server: int, path: str, message: str) -> None:
    with pytest.raises(TwinError, match=message):
        fetch_twin(settings(certs, server, path), MODULE)


def test_fetch_twin_deadline(certs: Certs, server: int) -> None:
    started = time.monotonic()
    with pytest.raises(TwinError):
        fetch_twin(settings(certs, server, "/slow"), MODULE, timeout_s=0.5)
    assert time.monotonic() - started < 1.4


def test_fetch_twin_unreachable(certs: Certs) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(TwinError, match="fetch failed"):
        fetch_twin(settings(certs, port, "/twin"), MODULE)


def test_fetch_twin_unknown_host(certs: Certs) -> None:
    unknown = TwinSettings(ca=certs.ca, endpoint="https://host.invalid/twin", identity=certs.client)
    with pytest.raises(TwinError, match="not found|deadline"):
        fetch_twin(unknown, MODULE, timeout_s=5)


def test_fetch_twin_missing_identity(certs: Certs, server: int) -> None:
    missing = TwinSettings(
        ca=certs.ca, endpoint=f"https://localhost:{server}/twin", identity="/nonexistent.pem"
    )
    with pytest.raises(TwinError, match="fetch failed"):
        fetch_twin(missing, MODULE)


def test_fetch_twin_sends_path_and_query(certs: Certs, server: int) -> None:
    twin = fetch_twin(settings(certs, server, "/twin?module=my-module&x=1"), MODULE)
    assert twin == GOOD["modules"][MODULE]
    assert Handler.seen_paths[-1] == "/twin?module=my-module&x=1"


@pytest.mark.parametrize("which", ["ca", "identity"])
def test_fetch_twin_corrupt_certificates(
    certs: Certs, server: int, tmp_path: Path, which: str
) -> None:
    corrupt = tmp_path / "corrupt.pem"
    corrupt.write_text("-----BEGIN CERTIFICATE-----\nnot base64\n-----END CERTIFICATE-----\n")
    files = {"ca": certs.ca, "identity": certs.client, which: str(corrupt)}
    endpoint = f"https://localhost:{server}/twin"
    broken = TwinSettings(ca=files["ca"], endpoint=endpoint, identity=files["identity"])
    with pytest.raises(TwinError, match="fetch failed"):
        fetch_twin(broken, MODULE)


def test_fetch_twin_sends_params_exactly_as_given(certs: Certs, server: int) -> None:
    fetch_twin(settings(certs, server, "/twin;v=1?x=1"), MODULE)
    assert Handler.seen_paths[-1] == "/twin;v=1?x=1"
