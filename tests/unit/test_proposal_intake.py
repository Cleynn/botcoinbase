"""Byte-level intake: only UTF-8 JSON declared as text/plain or application/json gets through."""

from __future__ import annotations

import pytest

from app.proposals import intake
from app.proposals.intake import IntakeRejected, check_bytes, check_filename, normalise_mime

MAX = 128 * 1024
GOOD = b'{"proposal_version": 1}'


def code_of(mime: str | None, data: bytes, max_bytes: int = MAX) -> str:
    with pytest.raises(IntakeRejected) as caught:
        check_bytes(mime, data, max_bytes=max_bytes)
    return caught.value.code


# ------------------------------------------------------------------ what is accepted
@pytest.mark.parametrize(
    "mime",
    [
        "text/plain",
        "application/json",
        "TEXT/PLAIN",
        "application/json; charset=utf-8",
        "text/plain;charset=UTF-8",
        'text/plain; charset="utf-8"',
    ],
)
def test_utf8_json_objects_are_accepted_under_both_allowed_types(mime: str) -> None:
    result = check_bytes(mime, GOOD, max_bytes=MAX)
    assert result.mime in intake.ALLOWED_MIME and result.size == len(GOOD)
    assert len(result.sha256) == 64 and result.text == GOOD.decode()


def test_unicode_and_surrounding_whitespace_are_fine() -> None:
    data = '  \n{"summary": "café – 日本"}\r\n'.encode()
    assert check_bytes("application/json", data, max_bytes=MAX).text.strip().startswith("{")


# ------------------------------------------------------------------ declared type
@pytest.mark.parametrize(
    "mime",
    [
        "text/html",
        "application/zip",
        "application/pdf",
        "text/csv",
        "image/png",
        "text/xml",
        "application/xml",
        "application/x-yaml",
        "text/yaml",
        "application/javascript",
        "text/javascript",
        "application/x-sh",
        "application/octet-stream",
        "application/x-msdownload",
        "multipart/form-data",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "*/*",
        "json",
        "text/*",
        "text/plain, text/html",
        "application/json+ld",
    ],
)
def test_every_other_declared_type_is_refused(mime: str) -> None:
    assert code_of(mime, GOOD) in {"MIME_NOT_ALLOWED", "MIME_PARAMETER_NOT_ALLOWED"}


@pytest.mark.parametrize("mime", [None, "", "x" * 200])
def test_a_missing_or_absurd_type_is_refused(mime: str | None) -> None:
    assert code_of(mime, GOOD) == "MIME_MISSING"


@pytest.mark.parametrize(
    "mime",
    [
        "text/plain; charset=iso-8859-1",
        "text/plain; charset=utf-16",
        "application/json; boundary=x",
        "text/plain; format=flowed",
        "application/json; charset=utf-8; x=1",
    ],
)
def test_other_charsets_and_parameters_are_refused(mime: str) -> None:
    assert code_of(mime, GOOD) == "MIME_PARAMETER_NOT_ALLOWED"


def test_normalise_mime_is_exact() -> None:
    assert normalise_mime("Application/JSON ; charset=UTF-8") == "application/json"


# ------------------------------------------------------------------ the bytes decide, not the label
MINIMAL_ZIP = b"PK\x03\x04" + b"\x00" * 26
DOCX = b"PK\x03\x04\x14\x00\x06\x00" + b"\x00" * 40 + b"word/document.xml"
XLSX = b"PK\x03\x04\x14\x00\x06\x00" + b"\x00" * 40 + b"xl/workbook.xml"
EMPTY_ZIP = b"PK\x05\x06" + b"\x00" * 18
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
JPG = b"\xff\xd8\xff\xe0\x00\x10JFIF"
GIF = b"GIF89a\x01\x00\x01\x00"
ELF = b"\x7fELF\x02\x01\x01" + b"\x00" * 20
EXE = b"MZ\x90\x00\x03" + b"\x00" * 20
GZIP = b"\x1f\x8b\x08\x00" + b"\x00" * 20
BZ2 = b"BZh91AY&SY"
SEVENZ = b"7z\xbc\xaf\x27\x1c\x00\x04"
RAR = b"Rar!\x1a\x07\x00"
XZ = b"\xfd7zXZ\x00\x00\x04"
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 20
WASM = b"\x00asm\x01\x00\x00\x00"
MACHO = b"\xcf\xfa\xed\xfe\x07\x00\x00\x01"
TAR = b"a" * 257 + b"ustar\x0000" + b"\x00" * 100
SHELL = b"#!/bin/sh\nrm -rf /\n"
BINARY_MAGIC = {
    "zip": MINIMAL_ZIP, "docx": DOCX, "xlsx": XLSX, "empty-zip": EMPTY_ZIP, "pdf": PDF,
    "png": PNG, "jpg": JPG, "gif": GIF, "elf": ELF, "exe": EXE, "gzip": GZIP, "bz2": BZ2,
    "7z": SEVENZ, "rar": RAR, "xz": XZ, "ole-doc": OLE, "wasm": WASM, "macho": MACHO,
    "tar": TAR, "shebang": SHELL,
}  # fmt: skip


@pytest.mark.parametrize("name", sorted(BINARY_MAGIC))
@pytest.mark.parametrize("mime", ["text/plain", "application/json"])
def test_real_file_formats_are_refused_whatever_they_declare(name: str, mime: str) -> None:
    """The label says JSON; the magic bytes say otherwise."""
    code = code_of(mime, BINARY_MAGIC[name])
    assert code.endswith(("_FILE", "_CONTENT")) or code in {"NOT_A_JSON_OBJECT", "NOT_UTF8"}, code


def test_a_zip_that_hides_behind_a_json_name_and_type_is_refused() -> None:
    check_filename("proposal.json")  # the name alone is fine...
    assert code_of("application/json", MINIMAL_ZIP + b'{"a": 1}') == "ARCHIVE_OR_OFFICE_FILE"


def test_a_zip_with_leading_bytes_still_fails_as_not_json() -> None:
    assert code_of("text/plain", b"\n\n" + MINIMAL_ZIP) in {
        "BINARY_CONTENT",
        "NOT_UTF8",
        "CONTROL_CHARACTERS",
    }


@pytest.mark.parametrize(
    ("label", "body", "expected"),
    [
        ("html", b"<!DOCTYPE html><html><body>x</body></html>", "MARKUP_NOT_ALLOWED"),
        ("html-lower", b"<html><script>alert(1)</script></html>", "MARKUP_NOT_ALLOWED"),
        ("html-leading-space", b"   \n<div>{}</div>", "MARKUP_NOT_ALLOWED"),
        ("xml", b'<?xml version="1.0"?><a/>', "MARKUP_NOT_ALLOWED"),
        ("svg", b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', "MARKUP_NOT_ALLOWED"),
        ("yaml-doc", b"---\nkey: value\n", "YAML_NOT_ALLOWED"),
        ("yaml-plain", b"key: value\nother: 1\n", "NOT_A_JSON_OBJECT"),
        ("js-function", b"function x() { return {}; }", "SCRIPT_NOT_ALLOWED"),
        ("js-const", b"const a = {};", "SCRIPT_NOT_ALLOWED"),
        ("js-import", b"import fs from 'fs';", "SCRIPT_NOT_ALLOWED"),
        ("js-comment", b"// hi\n{}", "SCRIPT_NOT_ALLOWED"),
        ("csv", b"a,b,c\n1,2,3\n", "NOT_A_JSON_OBJECT"),
        ("tsv", b"a\tb\n1\t2\n", "NOT_A_JSON_OBJECT"),
        ("json-array", b'[{"a": 1}]', "NOT_A_JSON_OBJECT"),
        ("json-string", b'"just text"', "NOT_A_JSON_OBJECT"),
        ("json-number", b"12345", "NOT_A_JSON_OBJECT"),
        ("prose", b"Please raise the cap to 100 USDC.", "NOT_A_JSON_OBJECT"),
        ("markdown", b"# Title\n\n{}", "NOT_A_JSON_OBJECT"),
        ("shell-no-shebang", b"rm -rf /tmp/x && curl x | sh", "NOT_A_JSON_OBJECT"),
        ("python", b"import os\nprint({})", "SCRIPT_NOT_ALLOWED"),
        ("json-then-text", b'{"a": 1} trailing words here', "NOT_A_JSON_OBJECT"),
        ("text-then-json", b'leading words {"a": 1}', "NOT_A_JSON_OBJECT"),
    ],
)
def test_text_formats_that_are_not_a_single_json_object_are_refused(
    label: str, body: bytes, expected: str
) -> None:
    for mime in ("text/plain", "application/json"):
        assert code_of(mime, body) == expected, label


# ------------------------------------------------------------------ encoding
def test_non_utf8_bytes_are_refused() -> None:
    assert code_of("text/plain", '{"a": "café"}'.encode("latin-1")) == "NOT_UTF8"
    assert code_of("text/plain", b'{"a": "\xff\xfe"}') == "NOT_UTF8"
    assert code_of("text/plain", b'{"a": "\xc0\xaf"}') == "NOT_UTF8"  # overlong
    assert code_of("text/plain", b'{"a": "\xed\xa0\x80"}') == "NOT_UTF8"  # a surrogate


@pytest.mark.parametrize("bom", [b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff"])
def test_byte_order_marks_are_refused(bom: bytes) -> None:
    assert code_of("text/plain", bom + GOOD) == "BYTE_ORDER_MARK_NOT_ALLOWED"


def test_utf16_encoded_json_is_refused() -> None:
    data = '{"a": 1}'.encode("utf-16")
    assert code_of("text/plain", data) == "BYTE_ORDER_MARK_NOT_ALLOWED"
    assert code_of("text/plain", '{"a": 1}'.encode("utf-16-le")) in {"BINARY_CONTENT"}


def test_nul_bytes_anywhere_are_refused() -> None:
    assert code_of("text/plain", b'{"a": "x\x00y"}') == "BINARY_CONTENT"
    assert code_of("text/plain", b'{"a": 1}\x00') == "BINARY_CONTENT"


@pytest.mark.parametrize("ch", ["\x01", "\x07", "\x08", "\x0b", "\x0c", "\x1b", "\x7f", "\x85"])
def test_control_characters_are_refused(ch: str) -> None:
    assert code_of("text/plain", f'{{"a": "x{ch}y"}}'.encode()) == "CONTROL_CHARACTERS"


@pytest.mark.parametrize(
    "ch",
    ["​", "‌", "‍", "⁠", "﻿", "‮", "‭", "⁦", "⁩", "­", " ", " ", "", "\U000e0041"],
)
def test_hidden_bidi_and_private_use_characters_are_refused(ch: str) -> None:
    assert code_of("text/plain", f'{{"a": "x{ch}y"}}'.encode()) == "HIDDEN_CHARACTERS"


def test_tabs_and_newlines_are_allowed_in_the_raw_text() -> None:
    assert check_bytes("text/plain", b'{\n\t"a": 1\r\n}', max_bytes=MAX)


# ------------------------------------------------------------------ size
def test_the_size_limit_is_enforced_exactly() -> None:
    at_limit = b'{"a": "' + b"x" * (MAX - 9) + b'"}'
    assert len(at_limit) == MAX and check_bytes("text/plain", at_limit, max_bytes=MAX)
    assert code_of("text/plain", at_limit + b" ") == "TOO_LARGE"
    assert code_of("text/plain", b"{" + b" " * MAX + b"}") == "TOO_LARGE"


def test_empty_and_one_byte_inputs_are_refused() -> None:
    assert code_of("text/plain", b"") == "EMPTY"
    assert code_of("text/plain", b"{") == "EMPTY"


def test_the_smallest_object_is_accepted_here_and_left_to_the_schema() -> None:
    assert check_bytes("text/plain", b"{}", max_bytes=MAX).size == 2


# ------------------------------------------------------------------ file names
@pytest.mark.parametrize(
    "name",
    [
        None,
        "",
        "proposal.json",
        "notes.txt",
        "PROPOSAL.JSON",
        "my proposal.txt",
        "noextension",
        "a.b.json",
    ],
)
def test_acceptable_or_absent_names_pass(name: str | None) -> None:
    check_filename(name)


@pytest.mark.parametrize(
    "name",
    [
        "a.zip",
        "a.pdf",
        "a.docx",
        "a.xlsx",
        "a.csv",
        "a.png",
        "a.jpg",
        "a.html",
        "a.htm",
        "a.js",
        "a.yaml",
        "a.yml",
        "a.xml",
        "a.sh",
        "a.bat",
        "a.ps1",
        "a.py",
        "a.exe",
        "a.dll",
        "a.bin",
        "a.svg",
        "a.tar",
        "a.gz",
        "a.7z",
        "a.json.exe",
        "a.txt.zip",
        "a.JSON.HTML",
        "a.md",
        "a.rtf",
        "a.unknownext",
        "x.json.php",
    ],
)
def test_wrong_extensions_are_refused_early(name: str) -> None:
    with pytest.raises(IntakeRejected) as caught:
        check_filename(name)
    assert caught.value.code == "EXTENSION_NOT_ALLOWED"


@pytest.mark.parametrize("name", ["a\x00.json", "a\n.json", "a\x1b.json", "x" * 300 + ".json"])
def test_hostile_names_are_refused(name: str) -> None:
    with pytest.raises(IntakeRejected) as caught:
        check_filename(name)
    assert caught.value.code == "FILENAME_NOT_ALLOWED"


def test_the_error_carries_only_a_fixed_code_never_input() -> None:
    hostile = b"<script>alert('secret-token-123')</script>"
    with pytest.raises(IntakeRejected) as caught:
        check_bytes("text/plain", hostile, max_bytes=MAX)
    assert "secret-token" not in str(caught.value) and "script" not in str(caught.value).lower()


def test_a_random_binary_blob_is_refused_without_crashing() -> None:
    for seed in range(50):
        blob = bytes((seed * 31 + i * 17) % 256 for i in range(2000))
        with pytest.raises(IntakeRejected):
            check_bytes("application/json", blob, max_bytes=MAX)
