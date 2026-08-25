"""Small, dependency-free App Store Connect API authentication helpers."""

import base64
import json
import subprocess
import time


P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
INVALID_SIGNATURE_MESSAGE = "OpenSSL returned an invalid ECDSA signature"


def b64url(value):
    """Encode bytes as unpadded base64url text for a JWT segment."""
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _read_der_length(der, offset):
    """Read one definite, minimally encoded DER length."""
    if offset >= len(der):
        raise ValueError(INVALID_SIGNATURE_MESSAGE)

    first_byte = der[offset]
    offset += 1
    if first_byte < 0x80:
        return first_byte, offset

    length_size = first_byte & 0x7F
    if length_size == 0 or offset + length_size > len(der):
        raise ValueError(INVALID_SIGNATURE_MESSAGE)

    encoded_length = der[offset : offset + length_size]
    if encoded_length[0] == 0:
        raise ValueError(INVALID_SIGNATURE_MESSAGE)

    length = int.from_bytes(encoded_length, "big")
    if length < 0x80:
        raise ValueError(INVALID_SIGNATURE_MESSAGE)
    return length, offset + length_size


def _read_ecdsa_integer(der, offset, sequence_end):
    """Read a canonical, positive P-256 ECDSA scalar from DER."""
    if offset >= sequence_end or der[offset] != 0x02:
        raise ValueError(INVALID_SIGNATURE_MESSAGE)

    length, offset = _read_der_length(der, offset + 1)
    value_end = offset + length
    if length == 0 or value_end > sequence_end:
        raise ValueError(INVALID_SIGNATURE_MESSAGE)

    value = der[offset:value_end]
    if value[0] & 0x80:
        raise ValueError(INVALID_SIGNATURE_MESSAGE)
    if len(value) > 1 and value[0] == 0 and not value[1] & 0x80:
        raise ValueError(INVALID_SIGNATURE_MESSAGE)

    if value[0] == 0:
        value = value[1:]

    scalar = int.from_bytes(value, "big")
    if scalar == 0 or scalar >= P256_ORDER:
        raise ValueError(INVALID_SIGNATURE_MESSAGE)
    return scalar.to_bytes(32, "big"), value_end


def der_to_jose(der):
    """Convert OpenSSL's DER ECDSA signature into JWT's fixed-width form."""
    if len(der) < 2 or der[0] != 0x30:
        raise ValueError(INVALID_SIGNATURE_MESSAGE)

    sequence_length, offset = _read_der_length(der, 1)
    sequence_end = offset + sequence_length
    if sequence_end != len(der):
        raise ValueError(INVALID_SIGNATURE_MESSAGE)

    r, offset = _read_ecdsa_integer(der, offset, sequence_end)
    s, offset = _read_ecdsa_integer(der, offset, sequence_end)
    if offset != sequence_end:
        raise ValueError(INVALID_SIGNATURE_MESSAGE)
    return r + s


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
