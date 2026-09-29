"""Static validation of the monitoring configuration (Prometheus, rules, Grafana, Compose, Caddy).

Enforces the Phase 3 rules: read-only and observational, internal only, no alert delivery, no
invented metrics, low-cardinality/non-sensitive labels, Grafana hardened, secrets required.
Exit 0 = all checks passed, 1 = at least one problem found. Never prints secret values.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import promql_parser
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import secret_problem  # noqa: E402
from app.monitoring.alerts import SEVERITIES  # noqa: E402
from app.monitoring.metrics import BY_FAMILY, CATALOGUE_NAMES, FORBIDDEN_LABEL_RE  # noqa: E402

MON = ROOT / "infra" / "monitoring"
DATASOURCE_UID = "prometheus"
EXPECTED_DASHBOARDS = {
    "td-overview": "Overview",
    "td-risk-failsafes": "Risk and Failsafes",
    "td-data-health": "Data Health",
    "td-execution": "Execution and Reconciliation",
    "td-vps": "VPS and Container Health",
}
EXTERNAL_PREFIXES = ("node_", "prometheus_", "process_", "python_", "container_", "scrape_")
EXTERNAL_EXACT = {"up", "ALERTS"}
HISTOGRAM_SUFFIXES = ("_bucket", "_sum", "_count")
ALLOWED_JOBS = {"prometheus", "tradingdots-app", "node", "cadvisor"}
ALLOWED_TARGETS = {"localhost:9090", "app:9464", "node-exporter:9100", "cadvisor:8080"}
ALLOWED_RULE_LABELS = {"severity", "component"}
ALLOWED_COMPONENTS = {"app", "node", "prometheus", "security", "monitoring"}
ALLOWED_ANNOTATIONS = {"summary", "description", "runbook"}
ALLOWED_ALERT_KEYS = {"alert", "expr", "for", "labels", "annotations"}
RECORDED_RE = re.compile(r"^[a-z_]+(:[a-z0-9_]+){2,}$")
MONITORING_SERVICES = ("prometheus", "grafana", "node-exporter", "cadvisor")
DEFAULT_ON = ("prometheus", "grafana", "node-exporter")
KNOWN_DEFAULT_SECRETS = {"admin", "grafana", "password", "changeme", "SW2YcwTIb9zpOOhoPsMm"}
DELIVERY_WORDS = re.compile(
    r"webhook|slack|pagerduty|opsgenie|smtp|e-?mail|telegram|discord|sns|coinbase|kill.?switch|"
    r"pause|resume|curl|https?://",
    re.IGNORECASE,
)
_GRAFANA_MACROS = {
    "$__rate_interval": "5m",
    "$__interval": "1m",
    "$__range": "1h",
    "$__interval_ms": "60000",
}


# ------------------------------------------------------------------ PromQL
def _walk(node: Any) -> list[Any]:
    found = [node]
    for attr in ("lhs", "rhs", "expr", "param", "vector_selector"):
        child = getattr(node, attr, None)
        if child is not None and hasattr(child, "prettify"):
            found.extend(_walk(child))
    for child in getattr(node, "args", None) or []:
        if hasattr(child, "prettify"):
            found.extend(_walk(child))
    return found


def known_metric(name: str, recorded: set[str]) -> bool:
    if name in CATALOGUE_NAMES or name in recorded or name in EXTERNAL_EXACT:
        return True
    if name.startswith(EXTERNAL_PREFIXES):
        return True
    for suffix in HISTOGRAM_SUFFIXES:
        base = name.removesuffix(suffix)
        if name.endswith(suffix) and base in BY_FAMILY and BY_FAMILY[base].kind == "histogram":
            return True
    return False


def check_expr(expr: str, recorded: set[str], where: str) -> list[str]:
    problems: list[str] = []
    text = expr
    for macro, value in _GRAFANA_MACROS.items():
        text = text.replace(macro, value)
    try:
        ast = promql_parser.parse(text)
    except ValueError as exc:
        return [f"{where}: invalid PromQL ({exc})"]
    for node in _walk(ast):
        if type(node).__name__ != "VectorSelector":
            continue
        if node.name is None:
            problems.append(f"{where}: selector without a metric name")
            continue
        if not known_metric(node.name, recorded):
            problems.append(f"{where}: unknown metric '{node.name}' (no such metric exists)")
        for matcher in node.matchers.matchers:
            if FORBIDDEN_LABEL_RE.search(matcher.name):
                problems.append(f"{where}: selects on sensitive label '{matcher.name}'")
    return problems


# ------------------------------------------------------------------ Prometheus
def check_prometheus_config(cfg: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    glob = cfg.get("global", {})
    if glob.get("scrape_interval") != "15s":
        problems.append("scrape_interval must be 15s")
    if glob.get("evaluation_interval") != "15s":
        problems.append("evaluation_interval must be 15s")
    for forbidden in ("remote_write", "remote_read"):
        if cfg.get(forbidden):
            problems.append(f"{forbidden} is not allowed")
    alerting = cfg.get("alerting") or {}
    if alerting.get("alertmanagers"):
        problems.append("alertmanagers are not allowed: alerts are displayed, never delivered")
    if cfg.get("rule_files") != [
        "/etc/prometheus/recording_rules.yml",
        "/etc/prometheus/alert_rules.yml",
    ]:
        problems.append("rule_files must be exactly the two provisioned rule files")
    jobs = {job["job_name"]: job for job in cfg.get("scrape_configs", [])}
    if set(jobs) != ALLOWED_JOBS:
        problems.append(f"scrape jobs must be exactly {sorted(ALLOWED_JOBS)}")
    for name, job in jobs.items():
        for static in job.get("static_configs", []):
            for target in static.get("targets", []):
                if target not in ALLOWED_TARGETS:
                    problems.append(
                        f"job {name}: target '{target}' is not an approved internal target"
                    )
        for banned in (
            "basic_auth",
            "authorization",
            "bearer_token",
            "bearer_token_file",
            "proxy_url",
            "tls_config",
            "file_sd_configs",
            "dns_sd_configs",
            "http_sd_configs",
            "kubernetes_sd_configs",
        ):
            if banned in job:
                problems.append(f"job {name}: '{banned}' is not allowed")
    return problems


def check_rules(
    alert_doc: dict[str, Any], recording_doc: dict[str, Any]
) -> tuple[list[str], set[str]]:
    problems: list[str] = []
    recorded: set[str] = set()
    for group in recording_doc.get("groups", []):
        for rule in group.get("rules", []):
            if set(rule) - {"record", "expr", "labels"}:
                problems.append(f"recording rule {rule.get('record')}: unexpected keys")
            name = rule.get("record", "")
            if not RECORDED_RE.fullmatch(name):
                problems.append(f"recording rule '{name}': name must be level:metric:operation")
            recorded.add(name)
    for group in recording_doc.get("groups", []):
        for rule in group.get("rules", []):
            problems += check_expr(
                str(rule["expr"]), recorded, f"recording rule {rule.get('record')}"
            )
    seen: set[str] = set()
    for group in alert_doc.get("groups", []):
        for rule in group.get("rules", []):
            name = rule.get("alert")
            if not name:
                problems.append("alert file may only contain alert rules")
                continue
            where = f"alert {name}"
            if name in seen:
                problems.append(f"{where}: duplicate name")
            seen.add(name)
            if not re.fullmatch(r"[A-Z][A-Za-z0-9]+", name):
                problems.append(f"{where}: name must be CamelCase")
            if set(rule) - ALLOWED_ALERT_KEYS:
                problems.append(
                    f"{where}: unexpected keys {sorted(set(rule) - ALLOWED_ALERT_KEYS)}"
                )
            labels = rule.get("labels") or {}
            if set(labels) - ALLOWED_RULE_LABELS:
                problems.append(f"{where}: labels must be within {sorted(ALLOWED_RULE_LABELS)}")
            if labels.get("severity") not in SEVERITIES:
                problems.append(f"{where}: severity must be one of {SEVERITIES}")
            if labels.get("component") not in ALLOWED_COMPONENTS:
                problems.append(f"{where}: component must be one of {sorted(ALLOWED_COMPONENTS)}")
            annotations = rule.get("annotations") or {}
            if set(annotations) != ALLOWED_ANNOTATIONS:
                problems.append(
                    f"{where}: annotations must be exactly {sorted(ALLOWED_ANNOTATIONS)}"
                )
            if annotations.get("runbook") != f"docs/alert-policy.md#{name.lower()}":
                problems.append(
                    f"{where}: runbook must point at docs/alert-policy.md#{name.lower()}"
                )
            for key, value in {**labels, **annotations}.items():
                if key != "runbook" and DELIVERY_WORDS.search(str(value)):
                    problems.append(
                        f"{where}: '{key}' mentions a delivery channel, URL or control action"
                    )
            problems += check_expr(str(rule.get("expr", "")), recorded, where)
    return problems, recorded


# ------------------------------------------------------------------ Grafana
def check_provisioning(datasources: dict[str, Any], providers: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    sources = datasources.get("datasources", [])
    if len(sources) != 1:
        problems.append("exactly one datasource is allowed")
    for source in sources:
        if source.get("uid") != DATASOURCE_UID or source.get("type") != "prometheus":
            problems.append(
                "the datasource must be the Prometheus datasource with uid 'prometheus'"
            )
        if source.get("url") != "http://prometheus:9090":
            problems.append("the datasource must use http://prometheus:9090 (internal)")
        if source.get("editable") is not False:
            problems.append("the datasource must not be editable")
        for banned in (
            "basicAuth",
            "basicAuthUser",
            "password",
            "secureJsonData",
            "withCredentials",
            "apiVersion_",
        ):
            if banned in source:
                problems.append(f"the datasource must not carry credentials ('{banned}')")
        if (source.get("jsonData") or {}).get("manageAlerts") is not False:
            problems.append("the datasource must not manage alerts")
    for provider in providers.get("providers", []):
        if (
            provider.get("disableDeletion") is not True
            or provider.get("allowUiUpdates") is not False
        ):
            problems.append("dashboards must be provisioned as non-deletable and non-updatable")
        if provider.get("options", {}).get("path") != "/var/lib/grafana-dashboards":
            problems.append("dashboard provider path must be /var/lib/grafana-dashboards")
    if len(providers.get("providers", [])) != 1:
        problems.append("exactly one dashboard provider is allowed")
    return problems


def _panels(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for panel in dashboard.get("panels", []):
        out.append(panel)
        out.extend(panel.get("panels", []))
    return out


def check_dashboard(dashboard: dict[str, Any], recorded: set[str], name: str) -> list[str]:
    problems: list[str] = []
    if dashboard.get("editable") is not False:
        problems.append(f"{name}: must not be editable")
    if dashboard.get("links"):
        problems.append(f"{name}: dashboard links are not allowed (no action or external links)")
    if dashboard.get("templating", {}).get("list"):
        problems.append(f"{name}: template variables are not used")
    refresh = str(dashboard.get("refresh", ""))
    if not re.fullmatch(r"(15|30)s|[1-9]\d*m", refresh):
        problems.append(f"{name}: refresh must be at least 15s")
    ids = [p.get("id") for p in _panels(dashboard)]
    if len(ids) != len(set(ids)):
        problems.append(f"{name}: duplicate panel ids")
    for panel in _panels(dashboard):
        title = f"{name}/{panel.get('title')}"
        if panel.get("type") in {"iframe", "news", "dashlist", "alertlist", "nodeGraph", "canvas"}:
            problems.append(f"{title}: panel type '{panel.get('type')}' is not allowed")
        if panel.get("type") == "text":
            content = str(panel.get("options", {}).get("content", ""))
            if re.search(
                r"<\s*(script|iframe|object|embed|form|a\b)|javascript:|https?://",
                content,
                re.IGNORECASE,
            ):
                problems.append(f"{title}: text panels must not contain HTML, links or scripts")
            continue
        if (panel.get("datasource") or {}).get("uid") != DATASOURCE_UID:
            problems.append(f"{title}: must use the provisioned datasource")
        for target in panel.get("targets", []):
            if (target.get("datasource") or {}).get("uid") != DATASOURCE_UID:
                problems.append(f"{title}: target must use the provisioned datasource")
            problems += check_expr(str(target.get("expr", "")), recorded, title)
        if panel.get("links") or "actions" in panel:
            problems.append(f"{title}: panel links/actions are not allowed")
    return problems


def check_dashboards(directory: Path, recorded: set[str]) -> list[str]:
    problems: list[str] = []
    found: dict[str, str] = {}
    for path in sorted(directory.glob("*.json")):
        dashboard = json.loads(path.read_text(encoding="utf-8"))
        found[dashboard.get("uid", "")] = dashboard.get("title", "")
        problems += check_dashboard(dashboard, recorded, path.name)
    if found != EXPECTED_DASHBOARDS:
        problems.append(f"dashboards must be exactly {EXPECTED_DASHBOARDS}")
    return problems


# ------------------------------------------------------------------ Compose and Caddy
def _volume_sources(service: dict[str, Any]) -> list[str]:
    return [str(v).split(":")[0] for v in service.get("volumes", [])]


def check_compose(compose: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    services = compose.get("services", {})
    networks = compose.get("networks", {})
    for name in MONITORING_SERVICES:
        svc = services.get(name)
        if svc is None:
            problems.append(f"service '{name}' is missing")
            continue
        if svc.get("ports"):
            problems.append(f"service '{name}' must not publish host ports")
        if svc.get("privileged") or svc.get("network_mode") or svc.get("pid"):
            problems.append(f"service '{name}' must not be privileged or share host namespaces")
        image = str(svc.get("image", ""))
        if ":" not in image or image.endswith(":latest"):
            problems.append(f"service '{name}' image must be pinned")
        for source in _volume_sources(svc):
            if "docker.sock" in source or source in {"/var/run", "/run", "/var/lib/docker"}:
                problems.append(f"service '{name}' must not mount the Docker socket or Docker data")
        for volume in svc.get("volumes", []):
            text = str(volume)
            if text.startswith("/") and not text.endswith(":ro"):
                problems.append(
                    f"service '{name}': host mount '{text.split(':')[0]}' must be read-only"
                )
        if not svc.get("healthcheck"):
            problems.append(f"service '{name}' needs a health check")
        if not svc.get("read_only"):
            problems.append(f"service '{name}' should have a read-only root filesystem")
    for name in DEFAULT_ON:
        if name in services and services[name].get("profiles"):
            problems.append(f"service '{name}' must run by default")
    cadvisor = services.get("cadvisor", {})
    if cadvisor and cadvisor.get("profiles") != ["cadvisor"]:
        problems.append("cadvisor must be optional (profile 'cadvisor')")

    def nets(name: str) -> set[str]:
        n = services.get(name, {}).get("networks", [])
        return set(n) if isinstance(n, (list, dict)) else set()

    expected = {
        "prometheus": {"mon_scrape", "mon_query"},
        "grafana": {"edge_grafana", "mon_query"},
        "node-exporter": {"mon_scrape"},
        "cadvisor": {"mon_scrape"},
    }
    for name, want in expected.items():
        if name in services and nets(name) != want:
            problems.append(f"service '{name}' networks must be exactly {sorted(want)}")
    if nets("app") & {"edge_grafana", "mon_query"} or nets("grafana") & {
        "edge_app",
        "backend",
        "mon_scrape",
    }:
        problems.append("the app and Grafana must share no network")
    for net in ("mon_scrape", "mon_query", "edge_grafana"):
        if not (networks.get(net) or {}).get("internal"):
            problems.append(f"network '{net}' must be internal")
    # Prometheus
    command = [str(part) for part in services.get("prometheus", {}).get("command", [])]
    if "--storage.tsdb.retention.time=30d" not in command:
        problems.append("Prometheus retention time must be 30d")
    if "--storage.tsdb.retention.size=15GB" not in command:
        problems.append("Prometheus retention size must be 15GB")
    for flag in (
        "--web.enable-lifecycle",
        "--web.enable-admin-api",
        "--web.enable-remote-write-receiver",
    ):
        if flag in command:
            problems.append(f"Prometheus must not enable {flag}")
    if not {"--no-web.enable-lifecycle", "--no-web.enable-admin-api"} <= set(command):
        problems.append("Prometheus must explicitly disable the lifecycle and admin APIs")
    # Grafana
    env = {
        str(k): str(v) for k, v in (services.get("grafana", {}).get("environment") or {}).items()
    }
    for key, value in {
        "GF_AUTH_ANONYMOUS_ENABLED": "false",
        "GF_USERS_ALLOW_SIGN_UP": "false",
        "GF_SECURITY_COOKIE_SECURE": "true",
        "GF_SECURITY_ALLOW_EMBEDDING": "false",
        "GF_PUBLIC_DASHBOARDS_ENABLED": "false",
        "GF_ALERTING_ENABLED": "false",
        "GF_UNIFIED_ALERTING_ENABLED": "false",
        "GF_METRICS_ENABLED": "false",
        "GF_ANALYTICS_REPORTING_ENABLED": "false",
        "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
    }.items():
        if env.get(key) != value:
            problems.append(f"Grafana {key} must be {value}")
    for key, variable in (
        ("GF_SECURITY_ADMIN_PASSWORD", "GRAFANA_ADMIN_PASSWORD"),
        ("GF_SECURITY_SECRET_KEY", "GRAFANA_SECRET_KEY"),
    ):
        value = env.get(key, "")
        if not value.startswith("${" + variable + ":?"):
            problems.append(
                f"Grafana {key} must be a required secret variable "
                f"(${{{variable}:?...}}) with no default"
            )
    if env.get("GF_SECURITY_COOKIE_SAMESITE") != "strict":
        problems.append("Grafana cookies must be SameSite=strict")
    return problems


def check_caddy(text: str) -> list[str]:
    problems: list[str] = []
    if "reverse_proxy grafana:3000" not in text:
        problems.append("the Grafana host must proxy grafana:3000")
    for target in ("prometheus", "node-exporter", "cadvisor", ":9090", ":9100", ":9464", ":8080"):
        if re.search(rf"reverse_proxy\s+\S*{re.escape(target)}", text):
            problems.append(f"Caddy must never proxy {target}")
    hosts = re.split(r"^\{\$TD_", text, flags=re.MULTILINE)[1:]
    for block in hosts:
        if "/metrics*" not in block.split("\n}\n")[0]:
            problems.append("every public host must return 404 for /metrics")
    grafana = next((b for b in hosts if b.startswith("GRAFANA_HOSTNAME")), "")
    if "/api/health*" not in grafana:
        problems.append("the Grafana host must hide /api/health")
    return problems


def check_env(env: dict[str, str], *, example: bool) -> list[str]:
    problems: list[str] = []
    for name in ("GRAFANA_ADMIN_PASSWORD", "GRAFANA_SECRET_KEY"):
        value = env.get(name)
        if not value:
            problems.append(f"{name} is missing")
        elif value in KNOWN_DEFAULT_SECRETS:
            problems.append(f"{name} is a well-known default")
        elif not example and (found := secret_problem(name, value)):
            problems.append(found)
    return problems


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def run() -> list[str]:
    problems = check_prometheus_config(_load(MON / "prometheus.yml"))
    rule_problems, recorded = check_rules(
        _load(MON / "alert_rules.yml"), _load(MON / "recording_rules.yml")
    )
    problems += rule_problems
    problems += check_provisioning(
        _load(MON / "grafana/provisioning/datasources/prometheus.yml"),
        _load(MON / "grafana/provisioning/dashboards/dashboards.yml"),
    )
    problems += check_dashboards(MON / "grafana/dashboards", recorded)
    problems += check_compose(_load(ROOT / "docker-compose.yml"))
    problems += check_caddy((ROOT / "infra/caddy/Caddyfile").read_text(encoding="utf-8"))
    return problems


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--env-file", type=Path, default=None, help="also validate the Grafana secrets in this file"
    )
    parser.add_argument(
        "--example", action="store_true", help="placeholders allowed (structure check only)"
    )
    args = parser.parse_args()
    problems = run()
    if args.env_file is not None:
        if not args.env_file.exists():
            problems.append(f"{args.env_file.name} not found")
        else:
            problems += check_env(_parse_env(args.env_file), example=args.example)
    for problem in problems:
        print(f"FAIL: {problem}")
    if problems:
        return 1
    print("PASS: monitoring configuration checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
