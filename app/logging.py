"""Logging setup with redaction of credentials, cookies, connection strings and full IPs."""

from __future__ import annotations

import logging
import logging.config
import re
import time
from pathlib import Path
from typing import Any

import yaml

from app.config import DEFAULT_CONFIG_DIR

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 ***"),
    (re.compile(r"(\b[a-zA-Z][\w+.-]*://[^:/\s@]+:)[^@\s]+@"), r"\1***@"),
    (
        re.compile(
            r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|set-cookie|cookie"
            r"|session[_-]?id|private[_-]?key)\b(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
        ),
        r"\1\2***",
    ),
    (re.compile(r"\b(?!0\.0\.0\.0\b|127\.)(\d{1,3}\.\d{1,3}\.\d{1,3})\.\d{1,3}\b"), r"\1.x"),
    (re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){3,7}[0-9a-fA-F]{1,4}\b"), "[ipv6-redacted]"),
)


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Redacts the fully formatted record, including exception text, and stamps UTC."""

    converter = staticmethod(time.gmtime)

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(level: str = "INFO", config_dir: Path = DEFAULT_CONFIG_DIR) -> None:
    with (config_dir / "logging.yaml").open(encoding="utf-8") as handle:
        config: dict[str, Any] = yaml.safe_load(handle)
    config["loggers"]["app"]["level"] = level
    logging.config.dictConfig(config)
