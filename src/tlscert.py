"""Self-signed certificate generation for the TLS listener.

Generated fresh at startup via the openssl CLI (no python-cryptography
dependency). Self-signed on purpose: it exercises both
`security.rejectInvalidCerts` paths — `true` hard-fails the call,
`false` warns and populates the tls timing/metadata scopes.
"""

import subprocess
import tempfile
from pathlib import Path

SAN = "DNS:tracedown-testbin,DNS:testbin,DNS:localhost,IP:127.0.0.1"


def generate_self_signed(directory: str | None = None) -> tuple[str, str]:
    """Returns (cert_path, key_path); raises on missing openssl."""
    out = Path(directory or tempfile.mkdtemp(prefix="testbin-tls-"))
    cert, key = out / "cert.pem", out / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(key), "-out", str(cert),
            "-days", "36500", "-nodes",
            "-subj", "/CN=tracedown-testbin",
            "-addext", f"subjectAltName={SAN}",
        ],
        check=True,
        capture_output=True,
    )
    return str(cert), str(key)
