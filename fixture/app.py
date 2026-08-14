"""Fairview Teller Console -- a fake credit-union back-office web app.

Deliberately styled like a 2004 enterprise intranet: nested table layout,
inline presentational attributes, no ids, no <label> associations, no
JavaScript. Serves as the automation target for the computer-use system.

Run with: python -m fixture.app --port 8123
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from decimal import Decimal

from flask import Flask, jsonify, redirect, render_template, request, url_for
from werkzeug.wrappers.response import Response


@dataclass(frozen=True)
class Member:
    """One fake credit-union member. All data is invented; no real PII."""

    name: str
    accounts: tuple[tuple[str, Decimal], ...]


MEMBERS: dict[str, Member] = {
    "12345": Member(
        name="Pat Ruiz",
        accounts=(("Savings", Decimal("1234.50")), ("Checking", Decimal("987.65"))),
    ),
    "67890": Member(
        name="Sam Okafor",
        accounts=(("Savings", Decimal("52.00")), ("Checking", Decimal("3410.19"))),
    ),
    "10001": Member(
        name="Lee Tanaka",
        accounts=(("Savings", Decimal("15000.00")), ("Checking", Decimal("42.07"))),
    ),
    "10002": Member(
        name="Morgan Patel",
        accounts=(("Savings", Decimal("6.25")),),
    ),
    "20340": Member(
        name="Casey Nguyen",
        accounts=(("Savings", Decimal("803.11")), ("Checking", Decimal("12000.00"))),
    ),
    "31337": Member(
        name="Jordan Weiss",
        accounts=(("Checking", Decimal("0.99")),),
    ),
    "44821": Member(
        name="Robin Delacroix",
        accounts=(("Savings", Decimal("77419.02")), ("Checking", Decimal("1.00"))),
    ),
    "50505": Member(
        name="Alex Bergstrom",
        accounts=(("Savings", Decimal("250.00")), ("Checking", Decimal("318.44"))),
    ),
}

# Fault-injection registry. Toggled at runtime via /__faults. Later slices
# add more faults (not_found_all, permission_denied, session_expired,
# interstitial) as new keys here plus simple checks in the views.
FAULTS: dict[str, bool] = {
    "slow_load": False,
}

SLOW_LOAD_SECONDS = 5.0


def format_money(amount: Decimal) -> str:
    """Format a balance like ``$1,234.50``."""
    return f"${amount:,.2f}"


def _parse_enabled(value: object) -> bool | None:
    """Coerce a JSON bool or form string into a bool, or None if invalid."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return None


def create_app() -> Flask:
    """Build the Flask app with all routes registered."""
    app = Flask(__name__)

    @app.get("/")
    def shell() -> str:
        return render_template("shell.html")

    @app.get("/nav")
    def nav() -> str:
        return render_template("nav.html")

    @app.get("/search")
    def search_get() -> str:
        q = request.args.get("q", "")
        miss = request.args.get("miss") == "1"
        return render_template("search.html", q=q, miss=miss)

    @app.post("/search")
    def search_post() -> Response:
        member_id = request.form.get("q1", "").strip()
        if member_id in MEMBERS:
            return redirect(url_for("member_detail", member_id=member_id))
        return redirect(url_for("search_get", q=member_id, miss="1"))

    @app.get("/member/<member_id>")
    def member_detail(member_id: str) -> str | Response:
        if FAULTS["slow_load"]:
            time.sleep(SLOW_LOAD_SECONDS)
        member = MEMBERS.get(member_id)
        if member is None:
            return redirect(url_for("search_get", q=member_id, miss="1"))
        rows = [(label, format_money(balance)) for label, balance in member.accounts]
        return render_template(
            "member.html", member_id=member_id, name=member.name, accounts=rows
        )

    @app.get("/reports")
    def reports() -> str:
        return render_template("stub.html", title="Reports")

    @app.get("/admin")
    def admin() -> str:
        return render_template("stub.html", title="Admin")

    @app.get("/__faults")
    def faults_get() -> Response:
        return jsonify(FAULTS)

    @app.post("/__faults")
    def faults_post() -> Response | tuple[Response, int]:
        fault: object
        enabled: object
        if request.is_json:
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict):
                return jsonify({"error": "body must be a JSON object"}), 400
            fault = payload.get("fault")
            enabled = payload.get("enabled")
        else:
            fault = request.form.get("fault")
            enabled = request.form.get("enabled")
        if not isinstance(fault, str) or fault not in FAULTS:
            return jsonify({"error": "unknown fault", "known": sorted(FAULTS)}), 400
        parsed = _parse_enabled(enabled)
        if parsed is None:
            return jsonify({"error": "enabled must be a boolean"}), 400
        FAULTS[fault] = parsed
        return jsonify(FAULTS)

    return app


def main() -> None:
    """CLI entry point: python -m fixture.app --port 8123."""
    parser = argparse.ArgumentParser(
        prog="fixture", description="Fairview Teller Console fixture app"
    )
    parser.add_argument("--port", type=int, default=8000, help="port to listen on")
    args = parser.parse_args()
    port: int = args.port
    create_app().run(host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
