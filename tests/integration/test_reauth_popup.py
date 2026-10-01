"""The password confirmation of a sensitive action is a dialog the browser opens as a pop-up.

The server side is unchanged (same forms, same routes); these tests pin the markup the script in
static/js/app.js relies on, and that the page still works when the script does not run."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from tests.conftest import GOOD_PASSWORD, ROOT, csrf_from

DIALOG = re.compile(r"<dialog[^>]*data-reauth-popup[^>]*>(.*?)</dialog>", re.S)


def test_a_launched_action_shows_the_password_dialog_until_it_is_confirmed(
    admin_client: TestClient,
) -> None:
    page = admin_client.get("/bot/pause/request").text
    found = DIALOG.search(page)
    assert found and '<dialog class="reauth" data-reauth-popup aria-labelledby="pw-h" open>' in page
    inner = found.group(1)
    # without JavaScript the open dialog is the form itself: it posts like before
    assert 'action="/bot/pause/reauth"' in inner and 'name="csrf_token"' in inner
    assert 'name="password" type="password"' in inner and "not confirmed" in inner
    assert 'href="/bot">Cancel</a>' in inner
    done = admin_client.post(
        "/bot/pause/reauth",
        data={"csrf_token": csrf_from(page), "password": GOOD_PASSWORD},
        follow_redirects=True,
    ).text
    assert "<dialog" not in done and "confirmed (single use)" in done  # no second pop-up
    assert "Step 2: type the phrase" in done


def test_the_hidden_fields_of_the_action_travel_inside_the_dialog(admin_client: TestClient) -> None:
    q = "levels=6&per_grid=20&invested=60&reserve=20&per_order=10"
    found = DIALOG.search(admin_client.get(f"/bot/trading/live/request?{q}").text)
    assert found and 'name="levels" value="6"' in found.group(1)
    assert 'action="/bot/trading/live/reauth"' in found.group(1)


def test_the_security_page_asks_on_demand(admin_client: TestClient) -> None:
    page = admin_client.get("/security").text
    assert '<dialog class="reauth" data-reauth-on-demand data-confirmed="no"' in page
    assert 'action="/security/users/revoke-sessions" class="stacked" data-needs-reauth>' in page
    token = csrf_from(page)
    admin_client.post("/security/reauth", data={"csrf_token": token, "password": GOOD_PASSWORD})
    assert 'data-confirmed="yes"' in admin_client.get("/security").text


def test_the_script_only_opens_dialogs_and_sends_nothing() -> None:
    script = (ROOT / "app/web/static/js/app.js").read_text()
    assert "showModal" in script and "data-reauth-popup" in script
    for word in ("fetch(", "XMLHttpRequest", "innerHTML", "eval(", ".value", "password ="):
        assert word not in script, word
