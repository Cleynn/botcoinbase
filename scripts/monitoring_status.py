"""Read-only monitoring status for the operator (run on the host).

Asks Prometheus, through `docker compose exec`, for its own health, scrape targets and firing
alerts, and prints a summary. It only issues GET requests to a fixed allowlist of read-only API
paths inside the Prometheus container: no writes, no reload, no admin API, no network access from
this machine, no shell. Exit code: 0 healthy, 1 needs attention, 2 could not query Prometheus.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from typing import Any

ALLOWED_PATHS = ("/-/healthy", "/api/v1/targets?state=active", "/api/v1/alerts")
OPTIONAL_JOBS = {"cadvisor"}  # only up when the optional profile is enabled
Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  fixed argument list, no shell
        list(command), capture_output=True, text=True, timeout=20, check=False
    )


def fetch(path: str, compose: Sequence[str], runner: Runner = _run) -> str:
    """GET one allowlisted Prometheus path from inside the container."""
    if path not in ALLOWED_PATHS:
        raise ValueError(f"path not allowed: {path}")
    result = runner(
        [*compose, "exec", "-T", "prometheus", "wget", "-qO-", f"http://127.0.0.1:9090{path}"]
    )
    if result.returncode != 0:
        raise RuntimeError("could not query Prometheus (is the stack running?)")
    return result.stdout


def summarise(targets: dict[str, Any], alerts: dict[str, Any]) -> dict[str, Any]:
    active = targets.get("data", {}).get("activeTargets", [])
    rows = [
        {
            "job": t.get("labels", {}).get("job", "?"),
            "health": t.get("health", "unknown"),
            "error": str(t.get("lastError", ""))[:120],
        }
        for t in active
    ]
    down = [r for r in rows if r["health"] != "up" and r["job"] not in OPTIONAL_JOBS]
    firing = [
        {
            "alert": a.get("labels", {}).get("alertname", "?"),
            "severity": a.get("labels", {}).get("severity", "?"),
            "state": a.get("state", "?"),
            "since": a.get("activeAt", ""),
        }
        for a in alerts.get("data", {}).get("alerts", [])
        if a.get("state") == "firing"
    ]
    serious = [f for f in firing if f["severity"] in {"critical", "high"}]
    return {
        "targets": rows,
        "targets_down": down,
        "firing": firing,
        "attention": bool(down or serious),
    }


def render(summary: dict[str, Any]) -> str:
    lines = ["Targets:"]
    lines += [
        f"  {r['job']:<16} {r['health']:<8} {r['error']}".rstrip() for r in summary["targets"]
    ] or ["  (none)"]
    lines.append("Firing alerts:")
    lines += [
        f"  [{f['severity']}] {f['alert']} since {f['since']}" for f in summary["firing"]
    ] or ["  none"]
    lines.append("STATUS: " + ("ATTENTION" if summary["attention"] else "OK"))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None, runner: Runner = _run) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--compose", default="docker compose", help="compose command (default: docker compose)"
    )
    args = parser.parse_args(argv)
    compose = args.compose.split()
    try:
        fetch("/-/healthy", compose, runner)
        targets = json.loads(fetch("/api/v1/targets?state=active", compose, runner))
        alerts = json.loads(fetch("/api/v1/alerts", compose, runner))
    except (RuntimeError, ValueError, subprocess.TimeoutExpired, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    summary = summarise(targets, alerts)
    print(json.dumps(summary, indent=2) if args.json else render(summary))
    return 1 if summary["attention"] else 0


if __name__ == "__main__":
    sys.exit(main())
