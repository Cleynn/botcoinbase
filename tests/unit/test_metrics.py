from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.parser import text_string_to_metric_families

from app.monitoring import metrics as m
from tests.conftest import FakeClock

ROOT = Path(__file__).resolve().parents[2]

# Every metric name the Master Contract asks for. Only ones with a real source may exist.
CONTRACT_NAMES = [
    "tradingdots_bot_info",
    "tradingdots_bot_last_successful_tick_timestamp_seconds",
    "tradingdots_bot_data_freshness_seconds",
    "tradingdots_bot_product_metadata_age_seconds",
    "tradingdots_bot_reconciliation_age_seconds",
    "tradingdots_bot_reconciliation_mismatches_total",
    "tradingdots_bot_risk_rejections_total",
    "tradingdots_bot_circuit_breaker_state",
    "tradingdots_bot_kill_switch_active",
    "tradingdots_bot_open_orders",
    "tradingdots_bot_order_intents_total",
    "tradingdots_bot_order_events_total",
    "tradingdots_bot_deployed_quote",
    "tradingdots_bot_protected_reserve_quote",
    "tradingdots_bot_realized_pnl_quote",
    "tradingdots_bot_unrealized_pnl_quote",
    "tradingdots_bot_drawdown_ratio",
    "tradingdots_bot_daily_loss_quote",
    "tradingdots_bot_grid_cycles_total",
    "tradingdots_bot_fee_paid_quote_total",
    "tradingdots_bot_api_requests_total",
    "tradingdots_bot_api_errors_total",
    "tradingdots_pair_candidates_total",
    "tradingdots_pair_state_total",
    "tradingdots_llm_review_packages_total",
    "tradingdots_llm_review_package_enabled",
    "tradingdots_llm_proposals_total",
    "tradingdots_llm_proposal_policy_rejections_total",
]
EXPECTED = {
    "tradingdots_bot_info",
    "tradingdots_live_trading_blocked",
    "tradingdots_http_requests_total",
    "tradingdots_http_request_duration_seconds",
    "tradingdots_db_up",
    "tradingdots_db_probe_duration_seconds",
    "tradingdots_db_schema_version",
    "tradingdots_db_expected_schema_version",
    "tradingdots_sessions_active",
    "tradingdots_users",
    "tradingdots_auth_events_total",
    "tradingdots_audit_events_total",
    "tradingdots_audit_last_event_timestamp_seconds",
    "tradingdots_audit_chain_ok",
    "tradingdots_audit_chain_events_verified",
    "tradingdots_audit_last_verified_timestamp_seconds",
    "tradingdots_metrics_scrapes_total",
    "tradingdots_metrics_last_scrape_timestamp_seconds",
    "tradingdots_collector_errors_total",
    # Phase 4: pair lifecycle (real database counts). The first two are contract names; the
    # third is an extension whose source (product metadata verification time) is real.
    "tradingdots_pair_candidates_total",
    "tradingdots_pair_state_total",
    "tradingdots_pair_metadata_age_seconds",
}
# Contract names that Phase 4 now publishes because a real source exists.
PUBLISHED_CONTRACT = {"tradingdots_pair_candidates_total", "tradingdots_pair_state_total"}
# Gauges that keep a contract `_total` name (they count rows, they are not counters).
GAUGES_NAMED_TOTAL = PUBLISHED_CONTRACT


def parse(body: bytes) -> dict[str, list[tuple[str, dict[str, str], float]]]:
    out: dict[str, list[tuple[str, dict[str, str], float]]] = {}
    for family in text_string_to_metric_families(body.decode()):
        out[family.name] = [(s.name, dict(s.labels), s.value) for s in family.samples]
    return out


# ------------------------------------------------------------------ catalogue
def test_catalogue_is_exactly_the_reviewed_set() -> None:
    assert m.CATALOGUE_NAMES == EXPECTED


def test_no_metric_is_invented_for_unfinished_bot_components() -> None:
    invented = [
        n
        for n in CONTRACT_NAMES
        if n != "tradingdots_bot_info" and n in m.CATALOGUE_NAMES and n not in PUBLISHED_CONTRACT
    ]
    assert invented == []
    assert not [
        n
        for n in m.CATALOGUE_NAMES
        if re.match(r"tradingdots_(bot_(?!info)|llm_|order|fill|reconcil|kill|breaker|risk)", n)
    ]
    # pair_* is allowed only for the reviewed set above
    assert {n for n in m.CATALOGUE_NAMES if n.startswith("tradingdots_pair_")} == {
        "tradingdots_pair_candidates_total",
        "tradingdots_pair_state_total",
        "tradingdots_pair_metadata_age_seconds",
    }


def test_catalogue_naming_and_documentation_rules() -> None:
    assert len({s.name for s in m.CATALOGUE}) == len(m.CATALOGUE)
    for spec in m.CATALOGUE:
        assert re.fullmatch(r"tradingdots_[a-z][a-z0-9_]*", spec.name), spec.name
        assert spec.help and spec.help.endswith("."), spec.name
        assert spec.kind in {"gauge", "counter", "histogram"}
        if spec.name not in GAUGES_NAMED_TOTAL:
            assert (spec.kind == "counter") == spec.name.endswith("_total"), spec.name
        if "timestamp" in spec.name:
            assert spec.name.endswith("_timestamp_seconds")
        if spec.kind == "histogram":
            assert spec.name.endswith("_seconds")


def test_catalogue_labels_are_few_bounded_and_never_sensitive() -> None:
    for spec in m.CATALOGUE:
        assert len(spec.labels) <= 2, spec.name
        for label in spec.labels:
            assert not m.FORBIDDEN_LABEL_RE.search(label), (spec.name, label)


@pytest.mark.parametrize(
    "label",
    [
        "user",
        "username",
        "user_id",
        "email",
        "ip",
        "client_ip",
        "remote_addr",
        "session",
        "session_id",
        "token",
        "cookie",
        "path",
        "url",
        "uri",
        "user_agent",
        "order_id",
        "client_order_id",
        "account_id",
        "password",
        "secret",
        "api_key",
        "hash",
        "trace_id",
        "error",
        "message",
        "timestamp",
        "request_id",
        "host",
        "address",
    ],
)
def test_forbidden_label_names_are_recognised(label: str) -> None:
    assert m.FORBIDDEN_LABEL_RE.search(label), label


@pytest.mark.parametrize(
    "label",
    ["route_template", "status_class", "event", "role", "mode", "version", "collector", "le"],
)
def test_the_labels_we_use_are_not_flagged(label: str) -> None:
    assert not m.FORBIDDEN_LABEL_RE.search(label)


def test_version_matches_pyproject() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["version"] == m.VERSION


# ------------------------------------------------------------------ helpers
def test_route_and_status_helpers() -> None:
    assert m.route_label("/security/sessions/revoke") == "/security/sessions/revoke"
    assert m.route_label(None) == "unmatched" and m.route_label("") == "unmatched"
    for hostile in (
        "/users/alice@example.com/x",
        "/" + "a" * 80,
        "no-slash",
        "/a b",
        "/<script>",
        "/x?token=1",
    ):
        assert m.route_label(hostile) == "other", hostile
    assert [m.status_class(c) for c in (200, 303, 404, 503, 99, 600)] == [
        "2xx",
        "3xx",
        "4xx",
        "5xx",
        "other",
        "other",
    ]
    assert m.clamp("a", ["a", "b"]) == "a" and m.clamp("zzz", ["a", "b"]) == "other"


# ------------------------------------------------------------------ sanitiser
def family(name: str, samples: list[tuple[dict[str, str], float]], kind: str = "gauge") -> Metric:
    metric = Metric(name, "doc.", kind)
    for labels, value in samples:
        metric.add_sample(name, labels, value)
    return metric


def test_unknown_families_are_dropped() -> None:
    kept, dropped = m.sanitize(
        [family("tradingdots_bot_open_orders", [({}, 0)]), family("something_else", [({}, 1)])]
    )
    assert kept == [] and set(dropped) == {
        "unknown_family:tradingdots_bot_open_orders",
        "unknown_family:something_else",
    }


def test_sensitive_and_unexpected_labels_are_dropped_per_sample() -> None:
    fam = family(
        "tradingdots_users",
        [
            ({"role": "ADMIN"}, 1),
            ({"role": "ADMIN", "username": "alice"}, 1),
            ({"ip": "10.0.0.1"}, 1),
        ],
    )
    kept, dropped = m.sanitize([fam])
    assert [s.labels for s in kept[0].samples] == [{"role": "ADMIN"}]
    assert dropped == ["bad_label:tradingdots_users"] * 2


def test_label_values_must_be_short_and_plain() -> None:
    fam = family(
        "tradingdots_users",
        [
            ({"role": "ADMIN"}, 1),
            ({"role": "x" * 80}, 1),
            ({"role": "a b"}, 1),
            ({"role": "<b>"}, 1),
        ],
    )
    kept, dropped = m.sanitize([fam])
    assert len(kept[0].samples) == 1 and dropped.count("bad_value:tradingdots_users") == 3


def test_cardinality_is_capped_per_family() -> None:
    fam = family(
        "tradingdots_auth_events", [({"event": f"e{i}"}, 1) for i in range(300)], "counter"
    )
    kept, dropped = m.sanitize([fam])
    assert len(kept[0].samples) == m.MAX_SERIES_PER_FAMILY
    assert dropped.count("cardinality:tradingdots_auth_events") == 200


def test_histogram_buckets_do_not_count_against_the_series_cap() -> None:
    fam = Metric("tradingdots_http_request_duration_seconds", "doc.", "histogram")
    for le in ("0.1", "0.5", "1.0", "+Inf"):
        fam.add_sample(
            "tradingdots_http_request_duration_seconds_bucket", {"route_template": "/", "le": le}, 1
        )
    kept, dropped = m.sanitize([fam])
    assert len(kept[0].samples) == 4 and dropped == []


def test_process_and_python_families_pass_with_their_own_labels_only() -> None:
    ok = family("process_cpu_seconds", [({}, 1.0)], "counter")
    gc = family("python_gc_objects_collected", [({"generation": "0"}, 1.0)], "counter")
    bad = family(
        "python_gc_objects_collected", [({"generation": "0", "user": "x"}, 1.0)], "counter"
    )
    kept, dropped = m.sanitize([ok, gc, bad])
    assert [f.name for f in kept if f.samples] == [
        "process_cpu_seconds",
        "python_gc_objects_collected",
    ] and dropped


# ------------------------------------------------------------------ instruments and rendering
def test_http_metrics_record_templates_never_raw_paths() -> None:
    metrics = m.Metrics(FakeClock())
    metrics.observe_request("/security", 200, 0.02)
    metrics.observe_request("/security", 500, 0.5)
    metrics.observe_request(None, 404, 0.001)
    metrics.observe_request("/users/bob@example.com", 200, 0.1)
    data = parse(metrics.render())
    counts = {
        (s[1]["route_template"], s[1]["status_class"]): s[2]
        for s in data["tradingdots_http_requests"]
        if s[0].endswith("_total")
    }
    assert counts == {
        ("/security", "2xx"): 1,
        ("/security", "5xx"): 1,
        ("unmatched", "4xx"): 1,
        ("other", "2xx"): 1,
    }


def test_render_is_valid_exposition_and_has_no_created_series() -> None:
    metrics = m.Metrics(FakeClock())
    body = metrics.render()
    assert b"_created" not in body
    assert body.startswith(b"# HELP") and body.endswith(b"\n")
    names = m.metric_names(body)
    assert "tradingdots_metrics_scrapes_total" in names and "process_cpu_seconds_total" in names


def test_histogram_le_values_are_validated_but_inf_survives() -> None:
    fam = Metric("tradingdots_http_request_duration_seconds", "doc.", "histogram")
    for le in ("0.005", "5.0", "+Inf", "1e-3", "<b>", "x" * 70):
        fam.add_sample(
            "tradingdots_http_request_duration_seconds_bucket", {"route_template": "/", "le": le}, 1
        )
    kept, dropped = m.sanitize([fam])
    assert [s.labels["le"] for s in kept[0].samples] == ["0.005", "5.0", "+Inf", "1e-3"] and len(
        dropped
    ) == 2


def test_a_real_histogram_survives_sanitising_with_its_inf_bucket() -> None:
    metrics = m.Metrics(FakeClock())
    metrics.observe_request("/", 200, 0.02)
    body = metrics.render().decode()
    assert 'le="+Inf"' in body and "tradingdots_http_request_duration_seconds_sum" in body


def test_render_tracks_scrapes_and_uses_the_injected_clock() -> None:
    clock = FakeClock()
    metrics = m.Metrics(clock)
    assert metrics.last_scrape_at is None
    metrics.render()
    metrics.render()
    data = parse(metrics.render())
    assert data["tradingdots_metrics_scrapes"][0][2] == 3
    assert (
        data["tradingdots_metrics_last_scrape_timestamp_seconds"][0][2] == clock.now().timestamp()
    )
    assert metrics.last_scrape_at == clock.now()


def test_collector_error_counter_has_a_fixed_series_set() -> None:
    metrics = m.Metrics(FakeClock())
    metrics.record_error("database")
    metrics.record_error("username=alice")  # arbitrary text collapses
    data = {
        s[1]["collector"]: s[2]
        for s in parse(metrics.render())["tradingdots_collector_errors"]
        if s[0].endswith("_total")
    }
    assert data == {"database": 1.0, "audit_chain": 0.0, "render": 0.0, "other": 1.0}


def test_a_family_a_collector_tries_to_smuggle_in_never_reaches_the_output() -> None:
    metrics = m.Metrics(FakeClock())

    class Rogue:
        def describe(self) -> list[Metric]:
            return []

        def collect(self) -> list[Metric]:
            fam = GaugeMetricFamily("tradingdots_rogue", "Rogue.", labels=["username"])
            fam.add_metric(["alice"], 1)
            return [fam]

    metrics.registry.register(Rogue())
    body = metrics.render()
    assert b"rogue" not in body and b"alice" not in body


def test_monitoring_docs_list_every_metric_and_the_reserved_ones_as_absent() -> None:
    doc = (ROOT / "docs" / "monitoring.md").read_text()
    for name in m.CATALOGUE_NAMES:
        assert f"`{name}`" in doc, f"{name} is not documented in docs/monitoring.md"
    reserved = doc.split("### Reserved, not implemented")[1].split("## Dashboards")[0]
    assert "None of these has a source yet" in reserved
    catalogue_table = doc.split("### Catalogue (every metric that exists)")[1].split(
        "### Label policy"
    )[0]
    assert not [
        n
        for n in CONTRACT_NAMES
        if n != "tradingdots_bot_info"
        and n not in PUBLISHED_CONTRACT
        and f"`{n}`" in catalogue_table
    ]
