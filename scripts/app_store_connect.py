"""Small, dependency-free App Store Connect API authentication helpers."""

import base64
import json
import subprocess
import tempfile
import time
from pathlib import Path


def b64url(value):
    """Encode bytes as unpadded base64url text for a JWT segment."""
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def der_to_jose(der):
    """Convert OpenSSL's DER ECDSA signature into JWT's fixed-width form."""
    if len(der) < 8 or der[0] != 0x30:
        raise ValueError("OpenSSL returned an invalid ECDSA signature")

    offset = 2
    if der[1] & 0x80:
        length_size = der[1] & 0x7F
        offset += length_size

    if offset >= len(der) or der[offset] != 0x02:
        raise ValueError("OpenSSL signature is missing r")
    r_length = der[offset + 1]
    r = der[offset + 2 : offset + 2 + r_length]
    offset += 2 + r_length

    if offset >= len(der) or der[offset] != 0x02:
        raise ValueError("OpenSSL signature is missing s")
    s_length = der[offset + 1]
    s = der[offset + 2 : offset + 2 + s_length]

    if len(r) == 0 or len(s) == 0:
        raise ValueError("OpenSSL returned an invalid ECDSA signature")
    return r[-32:].rjust(32, b"\0") + s[-32:].rjust(32, b"\0")


def make_token(key_path, key_id, issuer_id):
    """Create a short-lived ES256 JWT for the App Store Connect API."""
    header = b64url(json.dumps({"alg": "ES256", "kid": key_id, "typ": "JWT"}, separators=(",", ":")).encode())
    now = int(time.time())
    payload = b64url(
        json.dumps(
            {"iss": issuer_id, "iat": now, "exp": now + 900, "aud": "appstoreconnect-v1"},
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    signature = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(key_path)],
        input=signing_input,
        capture_output=True,
        check=True,
    ).stdout
    return f"{header}.{payload}.{b64url(der_to_jose(signature))}"


def make_token_from_private_key(private_key, key_id, issuer_id):
    """Create a token from in-memory PEM text without retaining it on disk."""
    key_path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".p8", delete=False) as key_file:
            key_file.write(private_key)
            key_path = Path(key_file.name)
        return make_token(key_path, key_id, issuer_id)
    finally:
        if key_path is not None:
            key_path.unlink(missing_ok=True)
