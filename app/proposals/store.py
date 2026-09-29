"""Proposal file storage: outside the web root, server-generated names, opaque bytes.

The bytes are never opened by anything but the host validator, never served, and never treated as
anything but data. Files are written once (atomic, durable, read-only) and named `<uuid>.proposal`
so that no server, tool or browser could mistake them for a page or a script.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Final

NAME: Final = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.proposal$"
)
_TMP: Final = re.compile(r"^\.[0-9a-f]{32}\.tmp$")


class StoreError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProposalStore:
    def __init__(self, directory: str) -> None:
        self._dir = Path(directory)

    def _path(self, name: str) -> Path:
        if not NAME.match(name):
            raise StoreError("BAD_NAME")
        return self._dir / name

    def ensure(self) -> None:
        self._dir.mkdir(mode=0o750, parents=True, exist_ok=True)

    def write(self, name: str, data: bytes) -> None:
        """Atomic, durable, read-only. Never overwrites an existing file."""
        final = self._path(name)
        self.ensure()
        if final.exists():
            raise StoreError("EXISTS")
        tmp = self._dir / f".{os.urandom(16).hex()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o440)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, final)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        dir_fd = os.open(self._dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def read(self, name: str, max_bytes: int) -> bytes:
        """Read a regular file (never through a symlink) no larger than `max_bytes`."""
        path = self._path(name)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError as exc:
            raise StoreError("MISSING") from exc
        except OSError as exc:
            raise StoreError("UNREADABLE") from exc
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise StoreError("NOT_A_FILE")
            if info.st_size > max_bytes:
                raise StoreError("TOO_LARGE")
            return handle.read(max_bytes + 1)

    def remove(self, name: str) -> bool:
        try:
            self._path(name).unlink()
        except FileNotFoundError:
            return False
        return True

    def stray_files(self, keep: set[str]) -> list[str]:
        """Names in the directory that no proposal row references (orphans and temp files)."""
        if not self._dir.is_dir():
            return []
        return sorted(
            p.name
            for p in self._dir.iterdir()
            if (NAME.match(p.name) and p.name not in keep) or _TMP.match(p.name)
        )

    def remove_stray(self, name: str) -> None:
        if not (NAME.match(name) or _TMP.match(name)):
            raise StoreError("BAD_NAME")
        (self._dir / name).unlink(missing_ok=True)
