"""Credential file guards and ES256 signing. Keys are generated in the tests; no real key exists."""

from __future__ import annotations

import base64
import json
import os
import pickle
from pathlib import Path
from typing import Any

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from app.exchange.cdp_signer import LIFETIME_SECONDS, CdpSigner
from app.exchange.credentials import (
    KEY_FILE_ENV,
    CredentialError,
    key_file_from_env,
    load_credentials,
)
from app.exchange.errors import ExchangeError

NAME = (
    "organizations/00000000-0000-4000-8000-000000000000"
    "/apiKeys/11111111-1111-4111-8111-222222222222"
)


def pem(curve: ec.EllipticCurve | None = None) -> str:
    key = ec.generate_private_key(curve or ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()


def write_key(tmp_path: Path, *, mode: int = 0o600, name: str = NAME, key: Any = None) -> str:
    path = tmp_path / "cdp_key.json"
    body = {"name": name, "privateKey": pem() if key is None else key}
    path.write_text(json.dumps(body))
    path.chmod(mode)
    return str(path)


def b64d(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


# ------------------------------------------------------------------ loading
def test_a_well_formed_private_file_loads_and_redacts_itself(tmp_path: Path) -> None:
    creds = load_credentials(write_key(tmp_path))
    assert creds.key_name == NAME and creds.key_hint == "..." + NAME[-6:]
    assert "Credentials(<redacted>)" == repr(creds)
    assert NAME not in repr(creds) and "PRIVATE" not in repr(creds)
    with pytest.raises(TypeError):
        pickle.dumps(creds)


@pytest.mark.parametrize("mode", [0o640, 0o644, 0o660, 0o604, 0o666])
def test_a_file_anyone_else_can_read_or_write_is_refused(tmp_path: Path, mode: int) -> None:
    with pytest.raises(CredentialError) as err:
        load_credentials(write_key(tmp_path, mode=mode))
    assert err.value.code == "KEY_FILE_PERMISSIONS"


def test_a_stricter_mode_is_fine(tmp_path: Path) -> None:
    assert load_credentials(write_key(tmp_path, mode=0o400)).key_name == NAME


def test_relative_missing_symlink_and_directory_paths_are_refused(tmp_path: Path) -> None:
    real = write_key(tmp_path)
    link = tmp_path / "link.json"
    link.symlink_to(real)
    cases = {
        "relative/path.json": "KEY_PATH_NOT_ABSOLUTE",
        "": "KEY_PATH_NOT_ABSOLUTE",
        str(tmp_path / "missing.json"): "KEY_FILE_MISSING",
        str(link): "KEY_FILE_NOT_REGULAR",
        str(tmp_path): "KEY_FILE_NOT_REGULAR",
    }
    for path, code in cases.items():
        with pytest.raises(CredentialError) as err:
            load_credentials(path)
        assert err.value.code == code, path


def test_oversized_empty_and_malformed_files_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "k.json"
    for content, code in (
        ("", "KEY_FILE_SIZE"),
        ("x" * 9000, "KEY_FILE_SIZE"),
        ("{not json", "KEY_FILE_FORMAT"),
        ("[1, 2]", "KEY_FILE_FORMAT"),
        ('{"name": "short"}', "KEY_NAME_FORMAT"),
    ):
        path.write_text(content)
        path.chmod(0o600)
        with pytest.raises(CredentialError) as err:
            load_credentials(str(path))
        assert err.value.code == code, content[:20]


@pytest.mark.parametrize(
    "name", ["short", "has space in it here", "x" * 300, "bad;chars;in;the;name"]
)
def test_a_malformed_key_name_is_refused(tmp_path: Path, name: str) -> None:
    with pytest.raises(CredentialError) as err:
        load_credentials(write_key(tmp_path, name=name))
    assert err.value.code == "KEY_NAME_FORMAT"


def test_a_key_that_is_not_p256_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CredentialError) as err:
        load_credentials(write_key(tmp_path, key=pem(ec.SECP384R1())))
    assert err.value.code == "KEY_NOT_P256"
    with pytest.raises(CredentialError) as err2:
        load_credentials(
            write_key(
                tmp_path, key="not-a-key " + "PRIVATE" + " KEY " + "garbage"
            )
        )
    assert err2.value.code == "KEY_FORMAT"
    with pytest.raises(CredentialError) as err3:
        load_credentials(write_key(tmp_path, key="just text"))
    assert err3.value.code == "KEY_FORMAT"


def test_errors_never_carry_file_content(tmp_path: Path) -> None:
    path = write_key(tmp_path, key="SECRET-PRIVATE-MARKER", mode=0o600)
    with pytest.raises(CredentialError) as err:
        load_credentials(path)
    assert "SECRET" not in str(err.value) and "SECRET" not in repr(err.value)


def test_no_path_configured_is_the_default() -> None:
    assert key_file_from_env({}) is None and key_file_from_env({KEY_FILE_ENV: ""}) is None
    assert key_file_from_env({KEY_FILE_ENV: "/run/secrets/k"}) == "/run/secrets/k"


# ------------------------------------------------------------------ signing
def signer(tmp_path: Path, now: float = 1_800_000_000.0) -> tuple[CdpSigner, Any]:
    creds = load_credentials(write_key(tmp_path))
    return CdpSigner(creds, clock=lambda: now), creds.private_key.public_key()


def verify(token: str, public: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    head, body, sig = token.split(".")
    raw = b64d(sig)
    assert len(raw) == 64
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    public.verify(der, f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))
    return json.loads(b64d(head)), json.loads(b64d(body))


def test_a_token_is_a_valid_es256_jwt_bound_to_one_request(tmp_path: Path) -> None:
    sign, public = signer(tmp_path)
    token = sign.bearer("GET", "api.coinbase.com", "/api/v3/brokerage/accounts")
    header, payload = verify(token, public)
    assert header["alg"] == "ES256" and header["typ"] == "JWT" and header["kid"] == NAME
    assert payload["sub"] == NAME and payload["iss"] == "cdp"
    assert payload["uri"] == "GET api.coinbase.com/api/v3/brokerage/accounts"
    assert payload["nbf"] == 1_800_000_000 and payload["exp"] - payload["nbf"] == LIFETIME_SECONDS


def test_the_signature_fails_for_any_other_key_or_tampering(tmp_path: Path) -> None:
    sign, _ = signer(tmp_path)
    token = sign.bearer("POST", "api.coinbase.com", "/api/v3/brokerage/orders")
    other = ec.generate_private_key(ec.SECP256R1()).public_key()
    with pytest.raises(InvalidSignature):
        verify(token, other)
    _, public = signer(tmp_path)
    head, body, sig = token.split(".")
    tampered = json.loads(b64d(body))
    tampered["uri"] = "POST api.coinbase.com/api/v3/brokerage/orders/batch_cancel"
    forged_body = base64.urlsafe_b64encode(json.dumps(tampered).encode()).rstrip(b"=").decode()
    with pytest.raises(InvalidSignature):
        verify(f"{head}.{forged_body}.{sig}", sign._credentials.private_key.public_key())  # noqa: SLF001


def test_every_token_has_a_fresh_nonce(tmp_path: Path) -> None:
    sign, public = signer(tmp_path)
    nonces = {
        verify(sign.bearer("GET", "api.coinbase.com", "/api/v3/brokerage/accounts"), public)[0][
            "nonce"
        ]
        for _ in range(20)
    }
    assert len(nonces) == 20


@pytest.mark.parametrize(
    ("method", "host", "path"),
    [
        ("DELETE", "api.coinbase.com", "/api/v3/brokerage/orders"),
        ("PUT", "api.coinbase.com", "/api/v3/brokerage/orders"),
        ("GET", "evil.example.com", "/api/v3/brokerage/accounts"),
        ("GET", "api.coinbase.com.evil.com", "/api/v3/brokerage/accounts"),
        ("GET", "api.coinbase.com", "/api/v3/brokerage"),
        ("GET", "api.coinbase.com", "/api/v2/accounts"),
        ("GET", "api.coinbase.com", "/api/v3/brokerage/accounts?limit=1"),
        ("GET", "api.coinbase.com", "/api/v3/brokerage/accounts#x"),
        ("GET", "api.coinbase.com", "/api/v3/brokerage/" + "a" * 400),
    ],
)
def test_only_the_intended_method_host_and_path_can_be_signed(
    tmp_path: Path, method: str, host: str, path: str
) -> None:
    sign, _ = signer(tmp_path)
    with pytest.raises(ExchangeError) as err:
        sign.bearer(method, host, path)
    assert err.value.code == "BAD_REQUEST"


def test_the_signer_redacts_itself(tmp_path: Path) -> None:
    sign, _ = signer(tmp_path)
    assert repr(sign) == "CdpSigner(<redacted>)" and NAME not in repr(sign)
    assert os.path.isfile(str(tmp_path / "cdp_key.json"))
