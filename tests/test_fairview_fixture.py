"""Route-level smoke tests for the Fairview 7-function surface.

These drive Flask's test client (no browser): login, inquiry, transfer,
new share, contact update, and the supervisor hold gate. Replay coverage
lives in test_fairview_replay.py / test_intent_gate.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import pytest
from flask.testing import FlaskClient

import fixture.app as fairview
from fixture.app import Share, create_app, format_money, reset_state


@pytest.fixture()
def client() -> Iterator[FlaskClient]:
    reset_state()
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client
    reset_state()


def _sign_on(
    client: FlaskClient, operator_id: str = "teller1", password: str = "password"
) -> None:
    response = client.post(
        "/signon", data={"o1": operator_id, "o2": password, "o3": "Main"}, follow_redirects=True
    )
    assert response.status_code == 200
    assert b"Teller Menu" in response.data


def _share(member_id: str, code: str) -> Share:
    found = fairview.MEMBERS[member_id].share(code)
    assert found is not None
    return found


def test_lookup_by_id_still_works_without_signon(client: FlaskClient) -> None:
    response = client.post("/search", data={"q1": "12345"}, follow_redirects=True)
    assert b"Member Details" in response.data
    assert b"Pat Ruiz" in response.data
    assert b"$1,234.50" in response.data
    assert b"S1" in response.data
    assert b"Funds Transfer" in response.data


def test_lookup_by_last_name(client: FlaskClient) -> None:
    response = client.post("/search", data={"q2": "Okafor"}, follow_redirects=True)
    assert b"Sam Okafor" in response.data
    assert b"67890" in response.data


def test_sign_on_and_bad_password(client: FlaskClient) -> None:
    bad = client.post("/signon", data={"o1": "teller1", "o2": "nope", "o3": ""})
    assert b"Sign-on failed" in bad.data
    _sign_on(client, "super1")
    menu = client.get("/menu")
    assert b"supervisor" in menu.data
    assert b"super1" in menu.data


def test_funds_transfer_posts_and_moves_money(client: FlaskClient) -> None:
    _sign_on(client)
    review = client.post(
        "/member/12345/transfer/review",
        data={"f1": "S1", "f2": "S2", "f3": "1.00", "f4": "test memo"},
        follow_redirects=True,
    )
    assert b"Transfer Review" in review.data
    assert b"Post Transfer" in review.data
    posted = client.post("/member/12345/transfer/post", follow_redirects=True)
    assert b"Transfer Posted" in posted.data
    assert b"CN-" in posted.data
    assert _share("12345", "S1").balance == Decimal("1233.50")
    assert _share("12345", "S2").balance == Decimal("988.65")


def test_transfer_insufficient_funds_is_validation(client: FlaskClient) -> None:
    _sign_on(client)
    response = client.post(
        "/member/12345/transfer/review",
        data={"f1": "S1", "f2": "S2", "f3": "99999", "f4": "too much"},
        follow_redirects=True,
    )
    assert b"The transaction could not be validated" in response.data
    assert b"Post Transfer" not in response.data
    assert _share("12345", "S1").balance == Decimal("1234.50")


def test_open_new_share_and_minimum_deposit(client: FlaskClient) -> None:
    _sign_on(client)
    rejected = client.post(
        "/member/12345/share/review",
        data={"n1": "Money Market", "n2": "1.00"},
        follow_redirects=True,
    )
    assert b"The request could not be validated" in rejected.data
    review = client.post(
        "/member/12345/share/review",
        data={"n1": "Money Market", "n2": "10.00"},
        follow_redirects=True,
    )
    assert b"Confirm New Share" in review.data
    posted = client.post("/member/12345/share/post", follow_redirects=True)
    assert b"Share Opened" in posted.data
    assert any(s.label == "Money Market" for s in fairview.MEMBERS["12345"].shares)


def test_update_contact(client: FlaskClient) -> None:
    _sign_on(client)
    response = client.post(
        "/member/12345/contact",
        data={
            "c1": "pat.ruiz@example.net",
            "c2": "555-0199",
            "c3": "99 Test Ave, Fairview",
        },
        follow_redirects=True,
    )
    assert b"Member Information Updated" in response.data
    assert fairview.MEMBERS["12345"].email == "pat.ruiz@example.net"
    assert fairview.MEMBERS["12345"].phone == "555-0199"


def test_hold_teller_is_supervisor_required(client: FlaskClient) -> None:
    _sign_on(client, "teller1")
    response = client.get("/member/12345/hold")
    assert b"SUPERVISOR OVERRIDE REQUIRED" in response.data
    assert b"is not authorized to perform this function" in response.data
    assert _share("12345", "S1").status == "Active"


def test_hold_supervisor_applies(client: FlaskClient) -> None:
    _sign_on(client, "super1")
    form = client.get("/member/12345/hold")
    assert b"Place Account Hold" in form.data
    review = client.post(
        "/member/12345/hold/review",
        data={"h1": "S1", "h2": "FRAUD", "h3": "demo hold"},
        follow_redirects=True,
    )
    assert b"Confirm Account Hold" in review.data
    posted = client.post("/member/12345/hold/post", follow_redirects=True)
    assert b"Account Hold Applied" in posted.data
    assert _share("12345", "S1").status == "HOLD"


def test_format_money_unchanged() -> None:
    assert format_money(Decimal("1234.50")) == "$1,234.50"
