"""Coinbase (CDP) API credentials: where they come from and how they are guarded.

The key lives in ONE file on the host, never in the repository, the environment, a log, an audit
row, a metric or the web tier. The file is the JSON Coinbase hands out
(`{"name": ..., "privateKey": ...}`) and is only accepted when it is safe to trust:

* an absolute path, a regular file (not a symlink, not a directory),
* owned by the running user and readable by that user only (mode 0600 or stricter),
* small, valid JSON, a well-formed key name and an EC P-256 private key.

Every failure is a `CredentialError` with a fixed code; no message ever contains file content.
The loaded object redacts itself in `repr`, cannot be pickled and keeps the key only as a parsed
key object, never as text. The web tier never imports this module and is never given the path.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

KEY_FILE_ENV: Final = "TD_COINBASE_KEY_FILE"
MAX_FILE_BYTES: Final = 8192
_NAME_RE: Final = re.compile(r"^[A-Za-z0-9/_.:-]{8,200}$")


class CredentialError(Exception):
    """The credential file is missing or unsafe. `code` is fixed; no file content is included."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Credentials:
    key_name: str = field(repr=False)
    private_key: Any = field(repr=False)  # a parsed EllipticCurvePrivateKey, never text

    def __repr__(self) -> str:
        return "Credentials(<redacted>)"

    def __reduce__(self) -> Any:
        raise TypeError("credentials cannot be serialised")

    @property
    def key_hint(self) -> str:
        """A short non-secret identifier for operator output (the tail of the key id)."""
        return "..." + self.key_name[-6:]


def load_credentials(path: str) -> Credentials:
    """Load and validate the key file, or raise `CredentialError`."""
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    if not path or not os.path.isabs(path) or "\x00" in path:
        raise CredentialError("KEY_PATH_NOT_ABSOLUTE")
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise CredentialError("KEY_FILE_MISSING") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise CredentialError("KEY_FILE_NOT_REGULAR")
    if info.st_mode & 0o077:
        raise CredentialError("KEY_FILE_PERMISSIONS")  # group or world can read or write it
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise CredentialError("KEY_FILE_OWNER")
    if not 0 < info.st_size <= MAX_FILE_BYTES:
        raise CredentialError("KEY_FILE_SIZE")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise CredentialError("KEY_FILE_FORMAT") from exc
    if not isinstance(data, dict):
        raise CredentialError("KEY_FILE_FORMAT")
    name, pem = data.get("name"), data.get("privateKey")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise CredentialError("KEY_NAME_FORMAT")
    if not isinstance(pem, str) or "PRIVATE KEY" not in pem:
        raise CredentialError("KEY_FORMAT")
    try:
        key = load_pem_private_key(pem.encode("ascii"), password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm, UnicodeEncodeError) as exc:
        raise CredentialError("KEY_FORMAT") from exc
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise CredentialError("KEY_NOT_P256")
    return Credentials(key_name=name, private_key=key)


def key_file_from_env(env: Mapping[str, str] | None = None) -> str | None:
    """The configured key path, or None when no credential is configured (the default)."""
    value = (os.environ if env is None else env).get(KEY_FILE_ENV, "")
    return value or None
