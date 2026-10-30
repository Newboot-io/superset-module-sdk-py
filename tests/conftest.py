from __future__ import annotations

import copy
from typing import Any

import pytest

BASE: dict[str, Any] = {
    "brokerUrl": "tcps://broker:8883",
    "configuration": {
        "inputs": ["output/telemetry/tenant-a/node-a/sensor/v"],
        "secretFiles": ["/vault/material/device-password.json"],
    },
    "mqtt": {
        "ca": "/etc/newboot/ca/mqtt-ca.crt",
        "identity": "/vault/material/mqtt-client.pem",
        "mode": "mutual-tls",
    },
    "target": {
        "clusterId": "cluster-a",
        "moduleId": "my-module",
        "nodeId": "node-a",
        "tenantConfigId": "tenant-a",
    },
    "twin": {
        "ca": "/etc/newboot/ca/twin-ca.crt",
        "endpoint": "https://twin/twin",
        "identity": "/vault/material/twin-client.pem",
    },
}


@pytest.fixture
def raw_config() -> dict[str, Any]:
    return copy.deepcopy(BASE)


class Certs:
    """A throwaway CA, a server certificate for localhost and a client PEM (cert, chain, key)."""

    def __init__(self, ca: str, server_cert: str, server_key: str, client: str) -> None:
        self.ca = ca
        self.server_cert = server_cert
        self.server_key = server_key
        self.client = client


@pytest.fixture(scope="session")
def certs(tmp_path_factory: pytest.TempPathFactory) -> Certs:
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    folder = tmp_path_factory.mktemp("certs")
    now = datetime.datetime.now(datetime.timezone.utc)

    def name(common: str) -> x509.Name:
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])

    def pem_key(key: ec.EllipticCurvePrivateKey) -> bytes:
        return key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (
        x509.CertificateBuilder()
        .subject_name(name("test-ca"))
        .issuer_name(name("test-ca"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )

    def leaf(common: str, server: bool) -> tuple[bytes, bytes]:
        key = ec.generate_private_key(ec.SECP256R1())
        usage = ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH
        builder = (
            x509.CertificateBuilder()
            .subject_name(name(common))
            .issuer_name(ca.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                critical=False,
            )
        )
        if server:
            builder = builder.add_extension(
                x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False
            )
        cert = builder.sign(ca_key, hashes.SHA256())
        return cert.public_bytes(serialization.Encoding.PEM), pem_key(key)

    ca_pem = ca.public_bytes(serialization.Encoding.PEM)
    (folder / "ca.crt").write_bytes(ca_pem)
    server_cert, server_key = leaf("localhost", server=True)
    (folder / "server.crt").write_bytes(server_cert)
    (folder / "server.key").write_bytes(server_key)
    client_cert, client_key = leaf("my-module-twin", server=False)
    (folder / "client.pem").write_bytes(client_cert + ca_pem + client_key)
    return Certs(
        ca=str(folder / "ca.crt"),
        server_cert=str(folder / "server.crt"),
        server_key=str(folder / "server.key"),
        client=str(folder / "client.pem"),
    )
