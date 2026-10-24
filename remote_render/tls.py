"""The gateway's self-signed certificate, and pinning it on the worker.

Workers connect to the main PC over HTTPS. There is no certificate
authority for a PC on a home network, so the gateway makes its own
certificate once, and a worker remembers its SHA-256 fingerprint when it
pairs (trust on first use, shown on both screens so they can be compared).
After that the worker only talks to that exact certificate: an address that
changes (DHCP) doesn't matter, an impostor does.
"""

import datetime
import hashlib
import socket
import ssl
from pathlib import Path


def ensure_certificate(directory: Path) -> tuple[Path, Path]:
    """(certificate, key) for the gateway, made on first use."""
    directory.mkdir(parents=True, exist_ok=True)
    cert, key = directory / "gateway.crt", directory / "gateway.key"
    if cert.exists() and key.exists():
        return cert, key
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    private = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"Clips Kitty on {socket.gethostname()}")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("clips-kitty-gateway")]), critical=False)
        .sign(private, hashes.SHA256())
    )
    key.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return cert, key


def fingerprint_of_file(cert: Path) -> str:
    der = ssl.PEM_cert_to_DER_cert(cert.read_text(encoding="ascii"))
    return hashlib.sha256(der).hexdigest()


def fingerprint_of_server(host: str, port: int, timeout: float = 10.0) -> str:
    """The certificate a server presents, whoever signed it."""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    # Read the certificate whoever signed it: that is the point (it is then
    # pinned by its fingerprint, not trusted through a CA).
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    return hashlib.sha256(der).hexdigest()


def short(fingerprint: str) -> str:
    """The first 16 hex digits in pairs, for people to compare by eye."""
    f = fingerprint.upper()[:16]
    return ":".join(f[i:i + 2] for i in range(0, len(f), 2))


def pinned_session(fingerprint: str):
    """A requests session that only accepts the certificate with this
    SHA-256 fingerprint (urllib3 checks it instead of a CA and a hostname)."""
    import requests
    import urllib3
    from requests.adapters import HTTPAdapter

    # urllib3 warns about any request without CA verification; this one is
    # pinned to one exact certificate, which is stricter than CA checks.
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    class _Pinned(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            kwargs["assert_fingerprint"] = fingerprint
            kwargs["cert_reqs"] = "CERT_NONE"
            return super().init_poolmanager(*args, **kwargs)

    s = requests.Session()
    s.mount("https://", _Pinned())
    s.verify = False
    return s
