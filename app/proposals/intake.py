"""Byte-level intake checks: file name, declared type, real bytes, size and encoding.

Only UTF-8 JSON is accepted, declared as `text/plain` or `application/json`. Both declarations must
hold the same thing: one JSON object. Everything else is refused by looking at the BYTES, never at
the file name or the declared type alone: archives, PDF, Office files, spreadsheets, CSV, images,
HTML, JavaScript, YAML, XML, shell scripts and executables all fail here. The checks do not parse
the JSON (the host validator does), so the web tier never interprets hostile content.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from typing import Final

ALLOWED_MIME: Final = ("text/plain", "application/json")
DENIED_EXTENSIONS: Final = frozenset(
    {
        "zip", "gz", "tgz", "bz2", "xz", "7z", "rar", "tar", "pdf", "doc", "docx", "docm", "xls",
        "xlsx", "xlsm", "ppt", "pptx", "odt", "ods", "csv", "tsv", "png", "jpg", "jpeg", "gif",
        "bmp", "webp", "svg", "ico", "tif", "tiff", "html", "htm", "xhtml", "js", "mjs", "ts",
        "yaml", "yml", "xml", "sh", "bash", "zsh", "bat", "cmd", "ps1", "py", "rb", "pl", "php",
        "exe", "dll", "so", "dylib", "bin", "msi", "jar", "class", "wasm", "com", "scr",
    }
)  # fmt: skip
ALLOWED_EXTENSIONS: Final = frozenset({"json", "txt"})

_MAGIC: Final[tuple[tuple[bytes, str], ...]] = (
    (b"PK\x03\x04", "ARCHIVE_OR_OFFICE_FILE"),
    (b"PK\x05\x06", "ARCHIVE_OR_OFFICE_FILE"),
    (b"PK\x07\x08", "ARCHIVE_OR_OFFICE_FILE"),
    (b"\x1f\x8b", "ARCHIVE_FILE"),
    (b"BZh", "ARCHIVE_FILE"),
    (b"7z\xbc\xaf\x27\x1c", "ARCHIVE_FILE"),
    (b"Rar!", "ARCHIVE_FILE"),
    (b"\xfd7zXZ\x00", "ARCHIVE_FILE"),
    (b"%PDF", "PDF_FILE"),
    (b"\xd0\xcf\x11\xe0", "OFFICE_FILE"),
    (b"\x89PNG", "IMAGE_FILE"),
    (b"GIF8", "IMAGE_FILE"),
    (b"\xff\xd8\xff", "IMAGE_FILE"),
    (b"II*\x00", "IMAGE_FILE"),
    (b"MM\x00*", "IMAGE_FILE"),
    (b"RIFF", "BINARY_FILE"),
    (b"\x7fELF", "EXECUTABLE_FILE"),
    (b"MZ", "EXECUTABLE_FILE"),
    (b"\xca\xfe\xba\xbe", "EXECUTABLE_FILE"),
    (b"\xcf\xfa\xed\xfe", "EXECUTABLE_FILE"),
    (b"\xce\xfa\xed\xfe", "EXECUTABLE_FILE"),
    (b"\x00asm", "BINARY_FILE"),
    (b"#!", "SCRIPT_FILE"),
)
_BOMS: Final = (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff")


class IntakeRejected(Exception):
    """The bytes are not an acceptable proposal. `code` is a fixed reason; nothing is echoed."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Intake:
    mime: str
    size: int
    sha256: str
    text: str


def normalise_mime(declared: str | None) -> str:
    """The declared media type: exactly `text/plain` or `application/json` (utf-8 charset only)."""
    if not declared or len(declared) > 100:
        raise IntakeRejected("MIME_MISSING")
    head, _sep, params = declared.partition(";")
    mime = head.strip().lower()
    if mime not in ALLOWED_MIME:
        raise IntakeRejected("MIME_NOT_ALLOWED")
    for param in filter(None, (p.strip() for p in params.split(";"))):
        name, _eq, value = param.partition("=")
        if name.strip().lower() != "charset" or value.strip().strip('"').lower() != "utf-8":
            raise IntakeRejected("MIME_PARAMETER_NOT_ALLOWED")
    return mime


def check_filename(name: str | None) -> None:
    """A client file name is never stored or trusted; a wrong extension is only an early refusal."""
    if not name:
        return
    if len(name) > 255 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise IntakeRejected("FILENAME_NOT_ALLOWED")
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in DENIED_EXTENSIONS or (ext and ext not in ALLOWED_EXTENSIONS):
        raise IntakeRejected("EXTENSION_NOT_ALLOWED")


def _classify_text(head: str) -> str:
    lowered = head.lstrip().lower()
    if lowered.startswith("<"):
        return "MARKUP_NOT_ALLOWED"
    if lowered.startswith(("---", "%yaml")):
        return "YAML_NOT_ALLOWED"
    if lowered.startswith(("function", "var ", "let ", "const ", "import ", "export ", "//")):
        return "SCRIPT_NOT_ALLOWED"
    return "NOT_A_JSON_OBJECT"


def check_bytes(declared_mime: str | None, data: bytes, *, max_bytes: int) -> Intake:
    """Validate declared type and the actual bytes. Returns the decoded text on success."""
    mime = normalise_mime(declared_mime)
    if len(data) < 2:
        raise IntakeRejected("EMPTY")
    if len(data) > max_bytes:
        raise IntakeRejected("TOO_LARGE")
    for magic, code in _MAGIC:
        if data.startswith(magic):
            raise IntakeRejected(code)
    if len(data) > 262 and data[257:262] == b"ustar":
        raise IntakeRejected("ARCHIVE_FILE")
    if data.startswith(_BOMS):
        raise IntakeRejected("BYTE_ORDER_MARK_NOT_ALLOWED")
    if b"\x00" in data:
        raise IntakeRejected("BINARY_CONTENT")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise IntakeRejected("NOT_UTF8") from exc
    for ch in text:
        category = unicodedata.category(ch)
        if category == "Cc" and ch not in "\n\r\t":
            raise IntakeRejected("CONTROL_CHARACTERS")
        if category in ("Cf", "Cs", "Co", "Zl", "Zp"):
            raise IntakeRejected("HIDDEN_CHARACTERS")
    stripped = text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        raise IntakeRejected(_classify_text(stripped[:64]))
    return Intake(mime, len(data), hashlib.sha256(data).hexdigest(), text)
