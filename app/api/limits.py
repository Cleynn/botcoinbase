"""Per-route request-body caps. Every other route keeps the small global cap."""

from __future__ import annotations

from typing import Final

IMPORT_CONFIRM_PATH: Final = "/review/proposals/import/confirm"
# A 128 KiB proposal, percent-encoded in the worst case (x3), plus the small form fields.
IMPORT_BODY_LIMIT: Final = 400 * 1024
BODY_LIMIT_OVERRIDES: Final = {IMPORT_CONFIRM_PATH: IMPORT_BODY_LIMIT}
