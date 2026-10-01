"""Static production-safety validator for env file, Docker Compose and Caddyfile.

Never prints secret values. Exit 0 = all checks passed, 1 = at least one problem found.
Reuses app.config so there is a single implementation of the production rules.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import ConfigError, load_settings, secret_problem  # noqa: E402

EGRESS_PROXY = "egress-proxy"
PUBLISHER = "caddy"
# The host's Apache owns the public 80/443 and proxies to Caddy on these loopback ports.
ALLOWED_PUBLISHED = {("8080", "tcp"), ("444", "tcp")}
LOOPBACK = "127.0.0.1"
EXTRA_SECRETS = (
    "POSTGRES_PASSWORD",
    "REDIS_PASSWORD",
    "GRAFANA_ADMIN_PASSWORD",
    "GRAFANA_SECRET_KEY",
    "TD_DB_APP_PASSWORD",
    "TD_DB_CTL_PASSWORD",
)
DUMMY_SECRET = "x" * 40  # only used in --example mode to skip placeholder detection


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def check_env(env: dict[str, str], *, example: bool, path: Path | None = None) -> list[str]:
    problems: list[str] = []
    if path is not None and not example and os.name == "posix" and path.stat().st_mode & 0o077:
        problems.append(f"{path.name} is readable by group/others (chmod 600 required)")
    td_env = {k: v for k, v in env.items() if k.startswith("TD_")}
    if example:
        td_env["TD_SECRET_KEY"] = DUMMY_SECRET
        td_env["TD_DB_PASSWORD"] = DUMMY_SECRET
    else:
        for name in EXTRA_SECRETS:
            found = secret_problem(name, env.get(name))
            if found:
                problems.append(found)
    if td_env.get("TD_ENVIRONMENT") != "production":
        problems.append("TD_ENVIRONMENT must be production")
    if not example:
        # Compose hands the app role's password to the app as TD_DB_PASSWORD.
        td_env["TD_DB_PASSWORD"] = env.get("TD_DB_APP_PASSWORD", "")
    # Load with the file's TD_* variables only: no ambient process environment is consulted.
    try:
        load_settings(td_env)
    except ConfigError as exc:
        problems.append(str(exc))
    return problems


def _published(entry: Any) -> tuple[str, str]:
    if isinstance(entry, dict):
        return str(entry.get("published", "")), str(entry.get("protocol", "tcp"))
    text = str(entry)
    protocol = "tcp"
    if "/" in text:
        text, protocol = text.rsplit("/", 1)
    parts = text.split(":")
    host = parts[-2] if len(parts) >= 2 else parts[0]
    return host, protocol


def _host_ip(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("host_ip", ""))
    parts = str(entry).rsplit("/", 1)[0].split(":")
    return ":".join(parts[:-2]) if len(parts) >= 3 else ""


def check_compose(compose: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    services: dict[str, Any] = compose.get("services", {})
    networks: dict[str, Any] = compose.get("networks", {}) or {}

    for name, svc in services.items():
        ports = svc.get("ports") or []
        if name != PUBLISHER and ports:
            problems.append(f"service '{name}' publishes host ports")
        if name == PUBLISHER:
            published = {_published(p) for p in ports}
            if published != ALLOWED_PUBLISHED:
                problems.append("caddy must publish exactly TCP 8080 and 444")
            # Docker bypasses the host firewall for published ports: only loopback is private.
            if any(_host_ip(p) != LOOPBACK for p in ports):
                problems.append("caddy must publish on 127.0.0.1 only")
        if svc.get("network_mode") == "host":
            problems.append(f"service '{name}' uses host networking")
        if svc.get("privileged"):
            problems.append(f"service '{name}' is privileged")
        if svc.get("pid") == "host":
            problems.append(f"service '{name}' shares the host PID namespace")
        for volume in svc.get("volumes") or []:
            if "docker.sock" in str(volume):
                problems.append(f"service '{name}' mounts the Docker socket")
        image = str(svc.get("image", ""))
        if image and "@sha256:" not in image and (":" not in image or image.endswith(":latest")):
            problems.append(f"service '{name}' image is unpinned or uses :latest")
        if "ALL" not in (svc.get("cap_drop") or []):
            problems.append(f"service '{name}' does not drop all capabilities")
        if "no-new-privileges:true" not in (svc.get("security_opt") or []):
            problems.append(f"service '{name}' lacks no-new-privileges")
        if not svc.get("profiles"):
            if not svc.get("restart"):
                problems.append(f"service '{name}' has no restart policy")
            one_shot = str(svc.get("restart")) == "no"  # migrate: runs to completion, no liveness
            if not svc.get("healthcheck") and not one_shot:
                problems.append(f"service '{name}' has no health check")

    app_user = str(services.get("app", {}).get("user", ""))
    if not app_user or app_user.split(":")[0] in {"0", "root"}:
        problems.append("app must run as a non-root user")

    for net_name, net in networks.items():
        if (net or {}).get("internal"):
            continue
        attached = {n for n, s in services.items() if net_name in (_names(s.get("networks")))}
        # Caddy (edge_public) and the allowlist egress proxy (egress_ext) are the only services
        # that may sit on a network with a route out.
        allowed = {PUBLISHER} if net_name != "egress_ext" else {EGRESS_PROXY}
        if attached - allowed:
            problems.append(f"non-internal network '{net_name}' is used by non-Caddy services")
    proxy = services.get(EGRESS_PROXY)
    if proxy is not None:
        if proxy.get("profiles") != ["discovery"]:
            problems.append("egress-proxy must run only under the discovery profile")
        if set(_names(proxy.get("networks"))) != {"egress_int", "egress_ext"}:
            problems.append("egress-proxy must join exactly egress_int and egress_ext")
    for name, svc in services.items():
        if "egress_int" in _names(svc.get("networks")) and name not in {
            EGRESS_PROXY,
            "pairs",
            "batch",
            "live",  # the live grid runner (host role, reads and orders go through the proxy)
        }:
            problems.append(f"service '{name}' must not join egress_int")
    problems += _check_review_volume(services)
    problems += _check_proposal_volume(services)
    return problems


def _check_review_volume(services: dict[str, Any]) -> list[str]:
    """Review packages: only `batch` may write the volume; `app` may only read it."""
    problems: list[str] = []
    for name, svc in services.items():
        for volume in svc.get("volumes") or []:
            text = str(volume)
            if not text.startswith("review_packages:"):
                continue
            read_only = text.endswith(":ro")
            if name == "batch" and read_only:
                problems.append("batch must mount review_packages read-write")
            elif name == "app" and not read_only:
                problems.append("app must mount review_packages read-only")
            elif name not in ("app", "batch"):
                problems.append(f"service '{name}' must not mount review_packages")
    return problems


def _check_proposal_volume(services: dict[str, Any]) -> list[str]:
    """Untrusted proposal files: only `app` (stores them) and `batch` (validates them) mount the
    volume, both read-write; nothing else may see it."""
    problems: list[str] = []
    for name, svc in services.items():
        for volume in svc.get("volumes") or []:
            text = str(volume)
            if not text.startswith("proposals:"):
                continue
            if name not in ("app", "batch"):
                problems.append(f"service '{name}' must not mount proposals")
            elif text.endswith(":ro"):
                problems.append(f"{name} must mount proposals read-write")
    return problems


def _names(networks: Any) -> list[str]:
    if isinstance(networks, dict):
        return list(networks)
    return list(networks or [])


def check_caddyfile(text: str) -> list[str]:
    problems: list[str] = []
    if not re.search(r"^\s*admin\s+off\s*$", text, re.MULTILINE):
        problems.append("Caddy admin API is not disabled (admin off)")
    for var in ("TD_APP_HOSTNAME", "TD_GRAFANA_HOSTNAME"):
        if "{$" + var + "}" not in text:
            problems.append(f"Caddyfile has no site block for {var}")
    if "reverse_proxy app:8000" not in text:
        problems.append("app host is not routed to app:8000")
    for target in ("prometheus", "postgres", "redis"):
        if re.search(rf"reverse_proxy\s+{target}\b", text):
            problems.append(f"Caddy proxies {target} publicly")
    for path in ("/healthz", "/metrics", "/docs", "/openapi.json"):
        if path not in text:
            problems.append(f"Caddyfile does not block {path}")
    # Behind Apache every request reaches Caddy from one gateway address: without these two the app
    # would see that address (or a client-chosen one) as the visitor and throttle logins globally.
    if not re.search(r"^\s*trusted_proxies_strict\s*$", text, re.MULTILINE):
        problems.append("Caddyfile does not read X-Forwarded-For strictly (trusted_proxies_strict)")
    if not re.search(
        r"reverse_proxy app:8000 \{\s*header_up X-Forwarded-For \{client_ip\}\s*\}", text
    ):
        problems.append("Caddy does not forward a single client address to the app")
    if re.search(r"auto_https\s+off", text) or re.search(r"Access-Control-Allow-Origin\s+\*", text):
        problems.append("Caddyfile weakens TLS or enables wildcard CORS")
    return problems


def run(env_file: Path, example: bool, compose_path: Path, caddyfile: Path) -> list[str]:
    problems: list[str] = []
    if not env_file.exists():
        problems.append(f"{env_file.name} not found (run scripts/bootstrap.sh, or use --example)")
    else:
        problems += check_env(parse_env_file(env_file), example=example, path=env_file)
    problems += check_compose(yaml.safe_load(compose_path.read_text(encoding="utf-8")))
    problems += check_caddyfile(caddyfile.read_text(encoding="utf-8"))
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--example", action="store_true", help="allow placeholder secrets")
    parser.add_argument("--compose", type=Path, default=ROOT / "docker-compose.yml")
    parser.add_argument("--caddyfile", type=Path, default=ROOT / "infra/caddy/Caddyfile")
    args = parser.parse_args()
    if args.example:
        print("NOTE: --example mode: placeholder secrets are allowed; NOT a production check.")
    problems = run(args.env_file, args.example, args.compose, args.caddyfile)
    for problem in problems:
        print(f"FAIL: {problem}")
    if problems:
        return 1
    print("PASS: all security configuration checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
