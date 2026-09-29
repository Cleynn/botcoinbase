"""Rotate a user's password from the host. Interactive terminal only; ends all their sessions.

Takes no arguments; the password is read with getpass. Run:
docker compose run --rm -it ctl python scripts/rotate_admin_password.py
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.auth.bootstrap_admin import BootstrapError, require_tty, rotate_password  # noqa: E402
from app.auth.password import PasswordService  # noqa: E402
from app.config import ConfigError, load_settings  # noqa: E402
from app.domain.models import SystemClock  # noqa: E402
from app.storage.database import SchemaError, Storage  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args:
        print("error: this command takes no arguments", file=sys.stderr)
        return 2
    try:
        require_tty()
        settings = load_settings()
        storage = Storage(settings.database)
        storage.check_schema()
        username = input("Username: ")
        password = getpass.getpass("New password (input hidden): ")
        if password != getpass.getpass("Repeat new password: "):
            print("error: passwords do not match", file=sys.stderr)
            return 1
        ended = rotate_password(
            storage=storage,
            passwords=PasswordService.create(settings.auth),
            settings=settings.auth,
            clock=SystemClock(),
            username=username,
            new_password=password,
        )
    except (BootstrapError, ConfigError, SchemaError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Password rotated; {ended} session(s) ended.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
