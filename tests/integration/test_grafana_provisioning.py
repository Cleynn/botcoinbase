"""Grafana provisioning and dashboards. Grafana itself cannot be run here (its download host is
blocked), so this validates the provisioning files, dashboard JSON and every PromQL expression
(the expressions are also executed against a real Prometheus in test_prometheus_scrape.py)."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.monitoring.metrics import CATALOGUE_NAMES
from tests.conftest import ROOT

MON = ROOT / "infra" / "monitoring"
DASHBOARDS = MON / "grafana" / "dashboards"
EXPECTED = {
    "overview.json": ("td-overview", "Overview"),
    "risk-and-failsafes.json": ("td-risk-failsafes", "Risk and Failsafes"),
    "data-health.json": ("td-data-health", "Data Health"),
    "execution.json": ("td-execution", "Execution and Reconciliation"),
    "vps-health.json": ("td-vps", "VPS and Container Health"),
}


@pytest.fixture(scope="module")
def vmc() -> Any:
    spec = importlib.util.spec_from_file_location(
        "verify_monitoring_config", ROOT / "scripts" / "verify_monitoring_config.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load(name: str) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((DASHBOARDS / name).read_text())
    return doc


def recorded(vmc: Any) -> set[str]:
    return set(
        vmc.check_rules(
            yaml.safe_load((MON / "alert_rules.yml").read_text()),
            yaml.safe_load((MON / "recording_rules.yml").read_text()),
        )[1]
    )


def panels(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return list(doc["panels"])


# ------------------------------------------------------------------ provisioning files
def test_the_five_required_dashboards_exist_and_nothing_else() -> None:
    assert {p.name for p in DASHBOARDS.glob("*.json")} == set(EXPECTED)
    for name, (uid, title) in EXPECTED.items():
        doc = load(name)
        assert (doc["uid"], doc["title"]) == (uid, title)


def test_datasource_is_a_single_internal_read_only_prometheus() -> None:
    doc = yaml.safe_load((MON / "grafana/provisioning/datasources/prometheus.yml").read_text())
    assert doc["prune"] is True and len(doc["datasources"]) == 1
    source = doc["datasources"][0]
    assert (source["uid"], source["type"], source["url"], source["access"]) == (
        "prometheus",
        "prometheus",
        "http://prometheus:9090",
        "proxy",
    )
    assert source["editable"] is False and source["jsonData"]["manageAlerts"] is False
    assert not {"basicAuth", "basicAuthUser", "password", "secureJsonData"} & set(source)


def test_dashboards_are_provisioned_read_only() -> None:
    provider = yaml.safe_load((MON / "grafana/provisioning/dashboards/dashboards.yml").read_text())[
        "providers"
    ][0]
    assert provider["disableDeletion"] is True and provider["allowUiUpdates"] is False
    assert (
        provider["type"] == "file" and provider["options"]["path"] == "/var/lib/grafana-dashboards"
    )


def test_compose_mounts_the_provisioning_files_read_only_at_the_expected_paths(
    compose: dict[str, Any],
) -> None:
    volumes = compose["services"]["grafana"]["volumes"]
    assert "./infra/monitoring/grafana/provisioning:/etc/grafana/provisioning:ro" in volumes
    assert "./infra/monitoring/grafana/dashboards:/var/lib/grafana-dashboards:ro" in volumes
    assert (
        compose["services"]["grafana"]["environment"]["GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH"]
        == "/var/lib/grafana-dashboards/overview.json"
    )
    assert (DASHBOARDS / "overview.json").exists()


def test_prometheus_mounts_only_the_reviewed_files(compose: dict[str, Any]) -> None:
    volumes = compose["services"]["prometheus"]["volumes"]
    for name in ("prometheus.yml", "recording_rules.yml", "alert_rules.yml"):
        assert f"./infra/monitoring/{name}:/etc/prometheus/{name}:ro" in volumes
        assert (MON / name).exists()


# ------------------------------------------------------------------ dashboard structure
@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_dashboard_is_read_only_and_structurally_sound(name: str) -> None:
    doc = load(name)
    assert doc["editable"] is False and doc["schemaVersion"] >= 39 and doc["timezone"] == "utc"
    assert (
        doc["refresh"] in {"30s", "1m", "5m"}
        and doc["links"] == []
        and doc["templating"]["list"] == []
    )
    assert "tradingdots" in doc["tags"] and doc["description"]
    ids = [p["id"] for p in panels(doc)]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_panels_fit_the_grid_and_never_overlap(name: str) -> None:
    boxes = [p["gridPos"] for p in panels(load(name))]
    for box in boxes:
        assert 0 <= box["x"] and box["x"] + box["w"] <= 24 and box["w"] >= 1 and box["h"] >= 1
    for i, a in enumerate(boxes):
        for b in boxes[i + 1 :]:
            apart = (
                a["x"] + a["w"] <= b["x"]
                or b["x"] + b["w"] <= a["x"]
                or a["y"] + a["h"] <= b["y"]
                or b["y"] + b["h"] <= a["y"]
            )
            assert apart, (name, a, b)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_data_panel_uses_the_provisioned_datasource_and_explains_its_source(
    name: str,
) -> None:
    for panel in panels(load(name)):
        if panel["type"] == "text":
            continue
        assert panel["datasource"] == {"type": "prometheus", "uid": "prometheus"}, panel["title"]
        assert panel["description"], f"{panel['title']} must say where its data comes from"
        for target in panel["targets"]:
            assert target["datasource"]["uid"] == "prometheus" and target["expr"].strip()


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_dashboard_states_it_is_read_only(name: str) -> None:
    text = next(p for p in panels(load(name)) if p["type"] == "text")
    assert (
        "Read-only" in text["options"]["content"]
        and "Actions live in the main dashboard" in text["options"]["content"]
    )


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_no_dashboard_contains_html_links_actions_or_external_content(name: str) -> None:
    raw = (DASHBOARDS / name).read_text().lower()
    for forbidden in (
        "<iframe",
        "<script",
        "javascript:",
        "http://",
        "https://",
        '"links": [{',
        "dashlist",
        "alertlist",
        '"actions"',
    ):
        assert forbidden not in raw, forbidden


# ------------------------------------------------------------------ metrics honesty
@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_query_parses_and_references_only_metrics_that_exist(vmc: Any, name: str) -> None:
    assert vmc.check_dashboard(load(name), recorded(vmc), name) == []


def test_no_dashboard_invents_a_metric_for_an_unbuilt_component(vmc: Any) -> None:
    import re

    used: set[str] = set()
    for name in EXPECTED:
        used |= set(re.findall(r"\btradingdots_[a-z_]+", json.dumps(load(name))))
    assert used <= CATALOGUE_NAMES | {
        f"{n}{s}" for n in CATALOGUE_NAMES for s in ("_bucket", "_sum", "_count")
    }
    assert not {
        u
        for u in used
        if u.startswith(("tradingdots_bot_", "tradingdots_pair", "tradingdots_llm"))
        and u != "tradingdots_bot_info"
    }


def test_the_execution_dashboard_shows_context_only_and_says_nothing_is_measured() -> None:
    doc = load("execution.json")
    data_panels = [p for p in panels(doc) if p["type"] != "text"]
    assert {p["title"] for p in data_panels} == {"Live trading", "Bot mode"}
    text = " ".join(p["options"]["content"] for p in panels(doc) if p["type"] == "text")
    assert "not measured" in text and "no placeholder" in text.lower()


def test_dashboards_use_only_recording_rules_that_are_defined(vmc: Any) -> None:
    import re

    defined = recorded(vmc)
    used = set()
    for name in EXPECTED:
        used |= set(re.findall(r"\b[a-z_]+:[a-z0-9_]+:[a-z0-9_]+", json.dumps(load(name))))
    assert used and used <= defined


def test_the_optional_cadvisor_panels_are_labelled_as_such() -> None:
    titles = [p for p in panels(load("vps-health.json")) if "cAdvisor" in p["title"]]
    assert len(titles) == 2 and all("optional" in p["description"].lower() for p in titles)


def test_the_real_configuration_passes_the_verifier(vmc: Any) -> None:
    assert vmc.run() == []


# ------------------------------------------------------------------ the verifier bites
@pytest.fixture
def board() -> dict[str, Any]:
    return load("overview.json")


def mutate(board: dict[str, Any], fn: Any) -> dict[str, Any]:
    copy_ = copy.deepcopy(board)
    fn(copy_)
    return copy_


def first_query(doc: dict[str, Any]) -> dict[str, Any]:
    return next(t for p in doc["panels"] for t in p.get("targets", []))


def test_verifier_rejects_an_invented_metric(vmc: Any, board: dict[str, Any]) -> None:
    doc = mutate(board, lambda d: first_query(d).update(expr="tradingdots_bot_deployed_quote"))
    assert any(
        "unknown metric 'tradingdots_bot_deployed_quote'" in p
        for p in vmc.check_dashboard(doc, recorded(vmc), "x")
    )


def test_verifier_rejects_selecting_on_a_sensitive_label(vmc: Any, board: dict[str, Any]) -> None:
    doc = mutate(board, lambda d: first_query(d).update(expr='tradingdots_users{username="alice"}'))
    assert any(
        "sensitive label 'username'" in p for p in vmc.check_dashboard(doc, recorded(vmc), "x")
    )


def test_verifier_rejects_unparseable_and_nameless_queries(vmc: Any, board: dict[str, Any]) -> None:
    assert any(
        "invalid PromQL" in p
        for p in vmc.check_dashboard(
            mutate(board, lambda d: first_query(d).update(expr="sum(")), recorded(vmc), "x"
        )
    )
    assert any(
        "without a metric name" in p
        for p in vmc.check_dashboard(
            mutate(board, lambda d: first_query(d).update(expr='{__name__=~"tradingdots_.*"}')),
            recorded(vmc),
            "x",
        )
    )


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        (lambda d: d.update(editable=True), "must not be editable"),
        (lambda d: d.update(links=[{"title": "x", "url": "https://evil.example"}]), "links"),
        (lambda d: d.update(refresh="1s"), "refresh"),
        (
            lambda d: d["panels"][1].update(datasource={"type": "prometheus", "uid": "other"}),
            "provisioned datasource",
        ),
        (
            lambda d: d["panels"].append(
                {"id": 999, "type": "iframe", "title": "bad", "targets": []}
            ),
            "not allowed",
        ),
        (lambda d: d["panels"][0]["options"].update(content="<script>x</script>"), "text panels"),
        (
            lambda d: d["panels"][0]["options"].update(content="[click](https://example.com)"),
            "text panels",
        ),
        (lambda d: d["panels"][1].update(links=[{"url": "x"}]), "links/actions"),
        (lambda d: d["panels"][2].update(id=d["panels"][1]["id"]), "duplicate panel ids"),
    ],
)
def test_verifier_rejects_unsafe_dashboard_changes(
    vmc: Any, board: dict[str, Any], change: Any, fragment: str
) -> None:
    problems = vmc.check_dashboard(mutate(board, change), recorded(vmc), "x")
    assert any(fragment in p for p in problems), problems


def test_verifier_rejects_unexpected_dashboard_sets(vmc: Any, tmp_path: Path) -> None:
    for path in DASHBOARDS.glob("*.json"):
        (tmp_path / path.name).write_text(path.read_text())
    (tmp_path / "overview.json").unlink()
    assert any("must be exactly" in p for p in vmc.check_dashboards(tmp_path, recorded(vmc)))


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        (lambda ds: ds["datasources"][0].update(secureJsonData={"password": "x"}), "credentials"),
        (lambda ds: ds["datasources"][0].update(url="https://prometheus.example"), "internal"),
        (lambda ds: ds["datasources"][0].update(editable=True), "editable"),
        (lambda ds: ds["datasources"][0]["jsonData"].update(manageAlerts=True), "manage alerts"),
        (lambda ds: ds["datasources"].append(dict(ds["datasources"][0])), "exactly one"),
    ],
)
def test_verifier_rejects_unsafe_datasource_changes(vmc: Any, change: Any, fragment: str) -> None:
    ds = yaml.safe_load((MON / "grafana/provisioning/datasources/prometheus.yml").read_text())
    providers = yaml.safe_load((MON / "grafana/provisioning/dashboards/dashboards.yml").read_text())
    change(ds)
    assert any(fragment in p for p in vmc.check_provisioning(ds, providers))


def test_verifier_rejects_editable_dashboard_providers(vmc: Any) -> None:
    ds = yaml.safe_load((MON / "grafana/provisioning/datasources/prometheus.yml").read_text())
    providers = yaml.safe_load((MON / "grafana/provisioning/dashboards/dashboards.yml").read_text())
    providers["providers"][0]["allowUiUpdates"] = True
    assert any("non-deletable" in p for p in vmc.check_provisioning(ds, providers))
