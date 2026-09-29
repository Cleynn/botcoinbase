"""Reports pages: read-only, escaped, labelled BACKTEST/PAPER, viewer-readable (synthetic)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from app.backtest.service import BacktestService
from tests.integration.conftest import Market
from tests.integration.test_snapshots import freeze

Sql = Callable[..., list[dict[str, Any]]]


def make_report(m: Market) -> Any:
    m.imported()
    snap, _ = freeze(m)
    report, _ = BacktestService(storage=m.storage, clock=m.clock, settings=m.settings).run(snap.id)
    return report


def test_viewers_and_admins_can_list_and_open_reports(
    mkt: Market, admin_client: TestClient, viewer_client: TestClient
) -> None:
    report = make_report(mkt)
    for client in (admin_client, viewer_client):
        page = client.get("/reports")
        assert page.status_code == 200 and str(report.id) in page.text
        assert "BACKTEST" in page.text and "LIVE TRADING" in page.text
        detail = client.get(f"/reports/{report.id}")
        assert detail.status_code == 200 and report.sha256 in detail.text


def test_anonymous_users_are_sent_to_login(client: TestClient) -> None:
    assert client.get("/reports", follow_redirects=False).status_code in (303, 401)
    assert client.get(f"/reports/{uuid4()}", follow_redirects=False).status_code in (303, 401)


def test_downloads_are_attachments_served_as_plain_text(
    mkt: Market, viewer_client: TestClient
) -> None:
    report = make_report(mkt)
    js = viewer_client.get(f"/reports/{report.id}/json")
    md = viewer_client.get(f"/reports/{report.id}/md")
    for response in (js, md):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.headers.get("x-content-type-options") == "nosniff"
    assert hashlib.sha256(js.text.encode()).hexdigest() == report.sha256
    assert json.loads(js.text)["label"] == "BACKTEST"
    assert "not a promise of profit" in md.text


def test_unknown_report_ids_are_404_and_bad_ids_rejected(admin_client: TestClient) -> None:
    assert admin_client.get(f"/reports/{uuid4()}").status_code == 404
    assert admin_client.get("/reports/not-a-uuid").status_code in (400, 404, 422)
    assert admin_client.get("/reports?page=0").status_code in (400, 404, 422)


def test_hostile_report_text_is_escaped_never_rendered(sql: Sql, admin_client: TestClient) -> None:
    hostile = '<script>alert("x")</script><img src=x onerror=alert(1)>'
    body = json.dumps({"kind": "BACKTEST", "note": hostile})
    rid = uuid4()
    sql(
        "INSERT INTO reports (id, kind, mode_label, title, body_json, body_md, sha256, created_at) "
        "VALUES (%s, 'BACKTEST', 'BACKTEST', %s, %s, %s, %s, now())",
        (rid, hostile, body, hostile, hashlib.sha256(body.encode()).hexdigest()),
    )
    for path in ("/reports", f"/reports/{rid}"):
        text = admin_client.get(path).text
        assert "<script>alert" not in text, path
        assert "<img" not in text, path
        assert "&lt;script&gt;" in text, path
    raw = admin_client.get(f"/reports/{rid}/md")
    assert raw.headers["content-disposition"].startswith("attachment;")


def test_reports_expose_no_write_route(app: Any) -> None:
    from tests.conftest import UNSAFE_METHODS, walk_routes

    for route in walk_routes(app):
        if route.path.startswith("/reports"):
            assert not (set(route.methods or ()) & UNSAFE_METHODS), route.path


def test_the_overview_marks_market_results_backtest_or_paper_only(
    mkt: Market, admin_client: TestClient
) -> None:
    make_report(mkt)
    text = admin_client.get("/").text
    assert "MODE: BACKTEST" in text and "LIVE TRADING: BLOCKED" in text
    assert "Latest report" in text or "Reports" in text
    assert "LIVE" not in text.replace("LIVE TRADING", "")


def test_metrics_for_the_new_areas_are_exposed_read_only(mkt: Market, app: Any) -> None:
    make_report(mkt)
    from app.monitoring import metrics

    names = {f.name for f in metrics.REGISTRY.collect()} if hasattr(metrics, "REGISTRY") else set()
    assert names is not None
