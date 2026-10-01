"""The VPS scripts parse, document themselves, and the offline check reports what is missing."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = sorted((ROOT / "scripts" / "vps").glob("*.sh"))
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash required")


def test_five_install_scripts_and_check_exist() -> None:
    names = {p.name for p in SCRIPTS}
    assert {
        "check.sh",
        "01-install-docker.sh",
        "02-install-prereqs.sh",
        "03-firewall.sh",
        "04-setup-env.sh",
        "05-first-start.sh",
    } <= names


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_parses_and_is_executable(script: Path) -> None:
    assert BASH is not None
    assert os.access(script, os.X_OK)
    result = subprocess.run([BASH, "-n", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_offline_check_names_fixes_and_never_prints_secrets() -> None:
    assert BASH is not None
    env = {**os.environ, "PATH": "/usr/bin:/bin", "TD_COINBASE_KEY_FILE": ""}
    result = subprocess.run(
        [BASH, str(ROOT / "scripts" / "vps" / "check.sh"), "--offline"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=60,
    )
    assert result.returncode in (0, 1)
    assert "== Summary ==" in result.stdout
    for line in result.stdout.splitlines():
        if line.startswith("[FAIL]"):
            assert "-> fix:" in result.stdout
    assert "PRIVATE KEY" not in result.stdout
