"""Create the first ADMIN. Interactive terminal only.

Takes no arguments; the password is read with getpass and never from argv, environment or a pipe.
Run: docker compose run --rm -it ctl python scripts/create_admin.py
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.auth.bootstrap_admin import BootstrapError, create_first_admin, require_tty  # noqa: E402
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
        username = input("Admin username: ")
        password = getpass.getpass("Password (input hidden): ")
        if password != getpass.getpass("Repeat password: "):
            print("error: passwords do not match", file=sys.stderr)
            return 1
        user = create_first_admin(
            storage=storage,
            passwords=PasswordService.create(settings.auth),
            settings=settings.auth,
            clock=SystemClock(),
            username=username,
            password=password,
        )
    except (BootstrapError, ConfigError, SchemaError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"ADMIN '{user.username}' created.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
