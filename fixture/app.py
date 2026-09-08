"""Fairview Teller Console -- a fake credit-union back-office web app.

Deliberately styled like a 2004 enterprise intranet: nested table layout,
inline presentational attributes, no ids, no <label> associations, no
JavaScript. Serves as the automation target for the computer-use system
and as the Adaptation-parity demo surface (sign-on, inquiry, transfer,
new share, contact update, supervisor hold) when MERIDIAN is unavailable.

Run with: python -m fixture.app --port 8123
"""

from __future__ import annotations

import argparse
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.wrappers.response import Response

# --------------------------------------------------------------------------- data


@dataclass
class Share:
    """One share on a member record. Codes are unique *per member*."""

    code: str
    label: str
    balance: Decimal
    status: str = "Active"

    def option_label(self) -> str:
        return f"{self.code} - {self.label}"


@dataclass
class Member:
    """One fake credit-union member. All data is invented; no real PII."""

    name: str
    last_name: str
    email: str
    phone: str
    address: str
    status: str = "Active"
    shares: list[Share] = field(default_factory=list)

    def share(self, code: str) -> Share | None:
        for item in self.shares:
            if item.code == code:
                return item
        return None

    def next_share_code(self) -> str:
        used = {s.code for s in self.shares}
        n = 1
        while f"S{n}" in used:
            n += 1
        return f"S{n}"


@dataclass(frozen=True)
class Operator:
    password: str
    role: str  # teller | supervisor
    display: str


def _seed_members() -> dict[str, Member]:
    """Canonical member roster. Balances on 12345 must stay $1,234.50 / $987.65
    so the original lookup_member_balance eval keeps its numbers."""
    return {
        "12345": Member(
            name="Pat Ruiz",
            last_name="Ruiz",
            email="pat.ruiz@example.com",
            phone="555-0101",
            address="14 Oak St, Fairview",
            shares=[
                Share("S1", "Savings", Decimal("1234.50")),
                Share("S2", "Checking", Decimal("987.65")),
            ],
        ),
        "67890": Member(
            name="Sam Okafor",
            last_name="Okafor",
            email="sam.okafor@example.com",
            phone="555-0102",
            address="88 Pine Ave, Fairview",
            shares=[
                Share("S1", "Savings", Decimal("52.00")),
                Share("S2", "Checking", Decimal("3410.19")),
            ],
        ),
        "10001": Member(
            name="Lee Tanaka",
            last_name="Tanaka",
            email="lee.tanaka@example.com",
            phone="555-0103",
            address="2 Cedar Ct, Fairview",
            shares=[
                Share("S1", "Savings", Decimal("15000.00")),
                Share("S2", "Checking", Decimal("42.07")),
            ],
        ),
        "10002": Member(
            name="Morgan Patel",
            last_name="Patel",
            email="morgan.patel@example.com",
            phone="555-0104",
            address="901 Birch Rd, Fairview",
            shares=[Share("S1", "Savings", Decimal("6.25"))],
        ),
        "20340": Member(
            name="Casey Nguyen",
            last_name="Nguyen",
            email="casey.nguyen@example.com",
            phone="555-0105",
            address="5 Maple Dr, Fairview",
            shares=[
                Share("S1", "Savings", Decimal("803.11")),
                Share("S2", "Checking", Decimal("12000.00")),
            ],
        ),
        "31337": Member(
            name="Jordan Weiss",
            last_name="Weiss",
            email="jordan.weiss@example.com",
            phone="555-0106",
            address="17 Walnut Ln, Fairview",
            shares=[Share("S2", "Checking", Decimal("0.99"))],
        ),
        "44821": Member(
            name="Robin Delacroix",
            last_name="Delacroix",
            email="robin.delacroix@example.com",
            phone="555-0107",
            address="44 Elm Blvd, Fairview",
            shares=[
                Share("S1", "Savings", Decimal("77419.02")),
                Share("S2", "Checking", Decimal("1.00")),
            ],
        ),
        "50505": Member(
            name="Alex Bergstrom",
            last_name="Bergstrom",
            email="alex.bergstrom@example.com",
            phone="555-0108",
            address="3 Spruce Way, Fairview",
            shares=[
                Share("S1", "Savings", Decimal("250.00")),
                Share("S2", "Checking", Decimal("318.44")),
            ],
        ),
    }


OPERATORS: dict[str, Operator] = {
    "teller1": Operator(password="password", role="teller", display="Teller One"),
    "super1": Operator(password="password", role="supervisor", display="Supervisor One"),
}

SHARE_TYPES: tuple[str, ...] = (
    "Regular Shares",
    "Share Draft",
    "Money Market",
    "Certificate",
)
HOLD_REASONS: tuple[str, ...] = ("FRAUD", "LEGAL", "PLEDGE", "OTHER")
BRANCHES: tuple[str, ...] = ("Main", "East Side", "West Side")
MIN_OPENING_DEPOSIT = Decimal("5.00")

# Fault-injection registry. Toggled at runtime via /__faults.
#
# Precedence when several faults are enabled at once:
#   1. session_expired -- intercepts content frames (not the chrome)
#   2. interstitial    -- intercepts GET /search
#   3. permission_denied / app_error / slow_load -- apply on /member/<id>
FAULTS: dict[str, bool] = {
    "slow_load": False,
    "permission_denied": False,
    "session_expired": False,
    "interstitial": False,
    "app_error": False,
    # UI drift stand-in: the search field's label renders as "Member Number".
    "renamed_label": False,
}

SLOW_LOAD_SECONDS = 5.0

# A member ID as accepted by the search form: one to ten ASCII digits.
MEMBER_ID_RE = re.compile(r"[0-9]{1,10}")
LAST_NAME_RE = re.compile(r"[A-Za-z' -]{1,40}")

MEMBERS: dict[str, Member] = {}
_CONFIRM_SEQ = 1000

# Routes that keep working during the session-expired interstitial: chrome,
# fault/reset plumbing, and the continue/dismiss handlers themselves.
_EXPIRED_EXEMPT = frozenset(
    {
        "shell",
        "nav",
        "continue_session",
        "dismiss_notice",
        "faults_get",
        "faults_post",
        "reset_state_route",
    }
)


def format_money(amount: Decimal) -> str:
    """Format a balance like ``$1,234.50``."""
    return f"${amount:,.2f}"


def reset_state() -> None:
    """Restore members, confirmation counter, and pending-txn session is the
    caller's job (cookies). Used by tests after mutating flows."""
    global MEMBERS, _CONFIRM_SEQ
    MEMBERS = _seed_members()
    _CONFIRM_SEQ = 1000


def _next_confirmation() -> str:
    global _CONFIRM_SEQ
    _CONFIRM_SEQ += 1
    return f"CN-{_CONFIRM_SEQ}"


def _parse_amount(raw: str) -> Decimal | None:
    """Strict dollars-and-cents. Reject rather than guess."""
    text = raw.strip().replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    if not value.is_finite() or value <= 0:
        return None
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        return None
    return value.quantize(Decimal("0.01"))


def _session_expired_page() -> str | None:
    """The Session Expired interstitial for the current request, if active.

    Returns None when the session_expired fault is off. The page carries the
    originally requested URL in a hidden ``u1`` field so POST /__continue can
    send the operator back where they were going.
    """
    if not FAULTS["session_expired"]:
        return None
    target = request.path
    if request.query_string:
        target = f"{request.path}?{request.query_string.decode('latin-1')}"
    return render_template("expired.html", u1=target)


def _is_safe_relative_path(target: str) -> bool:
    """True for a same-site relative path like ``/member/12345``.

    Rejects scheme-relative (``//host``) and backslash tricks so /__continue
    cannot be used as an open redirect.
    """
    return target.startswith("/") and not target.startswith("//") and "\\" not in target


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


def _operator_id() -> str | None:
    value = session.get("operator_id")
    return value if isinstance(value, str) else None


def _operator() -> Operator | None:
    oid = _operator_id()
    if oid is None:
        return None
    return OPERATORS.get(oid)


def _require_operator() -> Response | None:
    """Redirect to sign-on when no operator session is bound."""
    if _operator() is None:
        return redirect(url_for("signon_get"))
    return None


def _member_rows(member: Member) -> list[tuple[str, str, str, str]]:
    """Account | Balance | Share | Status — Balance stays immediately right of
    Account so lookup_member_balance's cell_right('Savings') still holds."""
    return [
        (s.label, format_money(s.balance), s.code, s.status) for s in member.shares
    ]


def _share_options(member: Member) -> list[tuple[str, str]]:
    return [(s.code, s.option_label()) for s in member.shares]


# --------------------------------------------------------------------------- app


def create_app() -> Flask:
    """Build the Flask app with all routes registered."""
    app = Flask(__name__)
    # Fixture-only; not a production secret. Sessions are the 2004 stand-in
    # for a teller sign-on cookie, not a security boundary.
    app.secret_key = "fairview-teller-fixture-not-a-secret"

    @app.before_request
    def _fault_session_expired() -> str | None:
        if request.endpoint in _EXPIRED_EXEMPT:
            return None
        return _session_expired_page()

    @app.get("/")
    def shell() -> str:
        main_src = "/menu" if _operator_id() else "/search"
        return render_template("shell.html", main_src=main_src)

    @app.get("/nav")
    def nav() -> str:
        op = _operator()
        oid = _operator_id()
        return render_template(
            "nav.html",
            signed_on=op is not None,
            operator_id=oid or "",
            role=op.role if op else "",
        )

    # ----- sign-on / menu -------------------------------------------------

    @app.get("/signon")
    def signon_get() -> str | Response:
        if _operator() is not None:
            return redirect(url_for("menu"))
        return render_template(
            "signon.html", err=request.args.get("err") == "1", branches=BRANCHES
        )

    @app.post("/signon")
    def signon_post() -> str | Response:
        operator_id = request.form.get("o1", "").strip()
        password = request.form.get("o2", "")
        branch = request.form.get("o3", "").strip()
        op = OPERATORS.get(operator_id)
        if op is None or op.password != password:
            # Never echo the password or say which field was wrong.
            return render_template("signon.html", err=True, branches=BRANCHES)
        session.clear()
        session["operator_id"] = operator_id
        session["role"] = op.role
        session["branch"] = branch
        return redirect(url_for("menu"))

    @app.get("/signoff")
    def signoff() -> Response:
        session.clear()
        return redirect(url_for("signon_get"))

    @app.get("/menu")
    def menu() -> str | Response:
        bounced = _require_operator()
        if bounced is not None:
            return bounced
        op = _operator()
        assert op is not None
        return render_template(
            "menu.html",
            operator_id=_operator_id(),
            display=op.display,
            role=op.role,
            branch=session.get("branch") or "—",
        )

    # ----- member inquiry -------------------------------------------------

    @app.get("/search")
    def search_get() -> str:
        if FAULTS["interstitial"]:
            return render_template("notice.html")
        q = request.args.get("q", "")
        miss = request.args.get("miss") == "1"
        err = request.args.get("err") == "1"
        by_name = request.args.get("by") == "name"
        return render_template(
            "search.html",
            q=q,
            miss=miss,
            err=err,
            by_name=by_name,
            id_label="Member Number" if FAULTS["renamed_label"] else "Member ID",
        )

    @app.post("/search")
    def search_post() -> str | Response:
        member_id = request.form.get("q1", "").strip()
        last_name = request.form.get("q2", "").strip()
        if member_id:
            if MEMBER_ID_RE.fullmatch(member_id) is None:
                return redirect(url_for("search_get", err="1"))
            if member_id in MEMBERS:
                return redirect(url_for("member_detail", member_id=member_id))
            return redirect(url_for("search_get", q=member_id, miss="1"))
        if last_name:
            if LAST_NAME_RE.fullmatch(last_name) is None:
                return redirect(url_for("search_get", err="1"))
            hits = [
                mid
                for mid, member in MEMBERS.items()
                if member.last_name.casefold() == last_name.casefold()
            ]
            if len(hits) == 1:
                return redirect(url_for("member_detail", member_id=hits[0]))
            if not hits:
                return redirect(url_for("search_get", q=last_name, miss="1", by="name"))
            rows = [(mid, MEMBERS[mid].name) for mid in hits]
            return render_template("search.html", matches=rows, q=last_name, id_label="Member ID")
        return redirect(url_for("search_get", err="1"))

    @app.get("/member/<member_id>")
    def member_detail(member_id: str) -> str | Response | tuple[str, int]:
        member = MEMBERS.get(member_id)
        if FAULTS["permission_denied"] and member is not None:
            return render_template("denied.html")
        if FAULTS["app_error"]:
            return render_template("error.html"), 500
        if FAULTS["slow_load"]:
            time.sleep(SLOW_LOAD_SECONDS)
        if member is None:
            return redirect(url_for("search_get", q=member_id, miss="1"))
        return render_template(
            "member.html",
            member_id=member_id,
            member=member,
            accounts=_member_rows(member),
        )

    # ----- funds transfer -------------------------------------------------

    @app.get("/member/<member_id>/transfer")
    def transfer_get(member_id: str) -> str | Response:
        bounced = _require_operator()
        if bounced is not None:
            return bounced
        member = MEMBERS.get(member_id)
        if member is None:
            return redirect(url_for("search_get", q=member_id, miss="1"))
        return render_template(
            "transfer.html",
            member_id=member_id,
            member=member,
            shares=_share_options(member),
            err=None,
        )

    @app.post("/member/<member_id>/transfer/review")
    def transfer_review(member_id: str) -> str | Response:
        bounced = _require_operator()
        if bounced is not None:
            return bounced
        member = MEMBERS.get(member_id)
        if member is None:
            return redirect(url_for("search_get", q=member_id, miss="1"))
        from_code = request.form.get("f1", "").strip()
        to_code = request.form.get("f2", "").strip()
        amount_raw = request.form.get("f3", "").strip()
        memo = request.form.get("f4", "").strip()
        src = member.share(from_code)
        dst = member.share(to_code)
        amount = _parse_amount(amount_raw)
        err = _transfer_error(src, dst, amount)
        if err is not None:
            return render_template(
                "transfer.html",
                member_id=member_id,
                member=member,
                shares=_share_options(member),
                err=err,
            )
        assert src is not None and dst is not None and amount is not None
        pending = {
            "from_code": from_code,
            "to_code": to_code,
            "from_label": src.option_label(),
            "to_label": dst.option_label(),
            "amount": str(amount),
            "memo": memo,
        }
        session["pending_transfer"] = pending
        return render_template(
            "transfer_review.html",
            member_id=member_id,
            member=member,
            pending=pending,
            amount_fmt=format_money(amount),
        )

    @app.post("/member/<member_id>/transfer/post")
    def transfer_post(member_id: str) -> str | Response:
        bounced = _require_operator()
        if bounced is not None:
            return bounced
        member = MEMBERS.get(member_id)
        pending = session.get("pending_transfer")
        if member is None or not isinstance(pending, dict):
            return redirect(url_for("transfer_get", member_id=member_id))
        src = member.share(str(pending.get("from_code", "")))
        dst = member.share(str(pending.get("to_code", "")))
        amount = _parse_amount(str(pending.get("amount", "")))
        err = _transfer_error(src, dst, amount)
        if err is not None or src is None or dst is None or amount is None:
            session.pop("pending_transfer", None)
            return render_template(
                "transfer.html",
                member_id=member_id,
                member=member,
                shares=_share_options(member),
                err=err or "The transaction could not be validated.",
            )
        src.balance -= amount
        dst.balance += amount
        session.pop("pending_transfer", None)
        confirmation = _next_confirmation()
        return render_template(
            "transfer_posted.html",
            member_id=member_id,
            member=member,
            pending=pending,
            amount_fmt=format_money(amount),
            confirmation=confirmation,
        )

    # ----- open new share -------------------------------------------------

    @app.get("/member/<member_id>/share")
    def share_get(member_id: str) -> str | Response:
        bounced = _require_operator()
        if bounced is not None:
            return bounced
        member = MEMBERS.get(member_id)
        if member is None:
            return redirect(url_for("search_get", q=member_id, miss="1"))
        return render_template(
            "share.html",
            member_id=member_id,
            member=member,
            share_types=SHARE_TYPES,
            err=None,
        )

    @app.post("/member/<member_id>/share/review")
    def share_review(member_id: str) -> str | Response:
        bounced = _require_operator()
        if bounced is not None:
            return bounced
        member = MEMBERS.get(member_id)
        if member is None:
            return redirect(url_for("search_get", q=member_id, miss="1"))
        share_type = request.form.get("n1", "").strip()
        deposit_raw = request.form.get("n2", "").strip()
        deposit = _parse_amount(deposit_raw)
        err = _share_error(share_type, deposit)
        if err is not None:
            return render_template(
                "share.html",
                member_id=member_id,
                member=member,
                share_types=SHARE_TYPES,
                err=err,
            )
        assert deposit is not None
        pending = {"share_type": share_type, "deposit": str(deposit)}
        session["pending_share"] = pending
        return render_template(
            "share_review.html",
            member_id=member_id,
            member=member,
            pending=pending,
            deposit_fmt=format_money(deposit),
        )

    @app.post("/member/<member_id>/share/post")
    def share_post(member_id: str) -> str | Response:
        bounced = _require_operator()
        if bounced is not None:
            return bounced
        member = MEMBERS.get(member_id)
        pending = session.get("pending_share")
        if member is None or not isinstance(pending, dict):
            return redirect(url_for("share_get", member_id=member_id))
        share_type = str(pending.get("share_type", ""))
        deposit = _parse_amount(str(pending.get("deposit", "")))
        err = _share_error(share_type, deposit)
        if err is not None or deposit is None:
            session.pop("pending_share", None)
            return render_template(
                "share.html",
                member_id=member_id,
                member=member,
                share_types=SHARE_TYPES,
                err=err or "The request could not be validated.",
            )
        code = member.next_share_code()
        member.shares.append(Share(code, share_type, deposit))
        session.pop("pending_share", None)
        confirmation = _next_confirmation()
        return render_template(
            "share_posted.html",
            member_id=member_id,
            member=member,
            share_type=share_type,
            share_code=code,
            deposit_fmt=format_money(deposit),
            confirmation=confirmation,
        )

    # ----- update contact -------------------------------------------------

    @app.get("/member/<member_id>/contact")
    def contact_get(member_id: str) -> str | Response:
        bounced = _require_operator()
        if bounced is not None:
            return bounced
        member = MEMBERS.get(member_id)
        if member is None:
            return redirect(url_for("search_get", q=member_id, miss="1"))
        return render_template("contact.html", member_id=member_id, member=member)

    @app.post("/member/<member_id>/contact")
    def contact_post(member_id: str) -> str | Response:
        bounced = _require_operator()
        if bounced is not None:
            return bounced
        member = MEMBERS.get(member_id)
        if member is None:
            return redirect(url_for("search_get", q=member_id, miss="1"))
        email = request.form.get("c1", "").strip()
        phone = request.form.get("c2", "").strip()
        address = request.form.get("c3", "").strip()
        member.email = email
        member.phone = phone
        member.address = address
        return render_template(
            "contact_updated.html", member_id=member_id, member=member
        )

    # ----- account hold (supervisor-only) --------------------------------

    @app.get("/member/<member_id>/hold")
    def hold_get(member_id: str) -> str | Response:
        bounced = _require_operator()
        if bounced is not None:
            return bounced
        op = _operator()
        assert op is not None
        member = MEMBERS.get(member_id)
        if member is None:
            return redirect(url_for("search_get", q=member_id, miss="1"))
        if op.role != "supervisor":
            return render_template(
                "supervisor.html",
                operator_id=_operator_id(),
                member_id=member_id,
            )
        return render_template(
            "hold.html",
            member_id=member_id,
            member=member,
            shares=_share_options(member),
            reasons=HOLD_REASONS,
            err=None,
        )

    @app.post("/member/<member_id>/hold/review")
    def hold_review(member_id: str) -> str | Response:
        bounced = _require_operator()
        if bounced is not None:
            return bounced
        op = _operator()
        assert op is not None
        if op.role != "supervisor":
            return render_template(
                "supervisor.html",
                operator_id=_operator_id(),
                member_id=member_id,
            )
        member = MEMBERS.get(member_id)
        if member is None:
            return redirect(url_for("search_get", q=member_id, miss="1"))
        code = request.form.get("h1", "").strip()
        reason = request.form.get("h2", "").strip()
        notes = request.form.get("h3", "").strip()
        target = member.share(code)
        err = _hold_error(target, reason)
        if err is not None:
            return render_template(
                "hold.html",
                member_id=member_id,
                member=member,
                shares=_share_options(member),
                reasons=HOLD_REASONS,
                err=err,
            )
        assert target is not None
        pending = {
            "code": code,
            "label": target.option_label(),
            "reason": reason,
            "notes": notes,
        }
        session["pending_hold"] = pending
        return render_template(
            "hold_review.html",
            member_id=member_id,
            member=member,
            pending=pending,
        )

    @app.post("/member/<member_id>/hold/post")
    def hold_post(member_id: str) -> str | Response:
        bounced = _require_operator()
        if bounced is not None:
            return bounced
        op = _operator()
        assert op is not None
        if op.role != "supervisor":
            return render_template(
                "supervisor.html",
                operator_id=_operator_id(),
                member_id=member_id,
            )
        member = MEMBERS.get(member_id)
        pending = session.get("pending_hold")
        if member is None or not isinstance(pending, dict):
            return redirect(url_for("hold_get", member_id=member_id))
        target = member.share(str(pending.get("code", "")))
        reason = str(pending.get("reason", ""))
        err = _hold_error(target, reason)
        if err is not None or target is None:
            session.pop("pending_hold", None)
            return render_template(
                "hold.html",
                member_id=member_id,
                member=member,
                shares=_share_options(member),
                reasons=HOLD_REASONS,
                err=err or "The request could not be validated.",
            )
        target.status = "HOLD"
        session.pop("pending_hold", None)
        confirmation = _next_confirmation()
        return render_template(
            "hold_posted.html",
            member_id=member_id,
            member=member,
            pending=pending,
            confirmation=confirmation,
        )

    # ----- stubs / plumbing ----------------------------------------------

    @app.post("/__continue")
    def continue_session() -> Response:
        FAULTS["session_expired"] = False
        target = request.form.get("u1", "")
        if not _is_safe_relative_path(target):
            target = url_for("search_get")
        return redirect(target)

    @app.post("/__dismiss")
    def dismiss_notice() -> Response:
        FAULTS["interstitial"] = False
        return redirect(url_for("search_get"))

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

    @app.route("/__reset", methods=["GET", "POST"])
    def reset_state_route() -> Response:
        reset_state()
        return jsonify({"ok": True, "members": sorted(MEMBERS)})

    return app


def _transfer_error(src: Share | None, dst: Share | None, amount: Decimal | None) -> str | None:
    if src is None or dst is None:
        return "The transaction could not be validated: unknown share."
    if src.code == dst.code:
        return "The transaction could not be validated: source and destination must differ."
    if amount is None:
        return "The transaction could not be validated: amount is not a valid dollar value."
    if src.status == "HOLD":
        return "The transaction could not be validated: source share is on hold."
    if src.balance < amount:
        return "The transaction could not be validated: insufficient funds."
    return None


def _share_error(share_type: str, deposit: Decimal | None) -> str | None:
    if share_type not in SHARE_TYPES:
        return "The request could not be validated: unknown share type."
    if deposit is None:
        return "The request could not be validated: initial deposit is not a valid dollar value."
    if deposit < MIN_OPENING_DEPOSIT:
        return (
            "The request could not be validated: initial deposit is below the "
            f"{format_money(MIN_OPENING_DEPOSIT)} minimum."
        )
    return None


def _hold_error(target: Share | None, reason: str) -> str | None:
    if target is None:
        return "The request could not be validated: unknown share."
    if reason not in HOLD_REASONS:
        return "The request could not be validated: unknown reason code."
    if target.status == "HOLD":
        return "The request could not be validated: share is already on hold."
    return None


def main() -> None:
    """CLI entry point: python -m fixture.app --port 8123."""
    parser = argparse.ArgumentParser(
        prog="fixture", description="Fairview Teller Console fixture app"
    )
    parser.add_argument("--port", type=int, default=8000, help="port to listen on")
    args = parser.parse_args()
    port: int = args.port
    create_app().run(host="127.0.0.1", port=port)


reset_state()


if __name__ == "__main__":
    main()
