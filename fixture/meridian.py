"""MERIDIAN-shaped local fixture: a deterministic stand-in for the live
MERIDIAN CORE console at web-sample.interface-hiring.com.

Why this exists: the live console is shared and stateful, and Place Account
Hold is consequential inside it (a held share stays held). Repeated chaos and
evaluation runs therefore need a local target whose markup is close enough to
the live one that the SAME capability artifacts replay against it with only
the entry URL swapped. Page structure, headings, labels, form field names,
and the hold flow (form -> review -> post) mirror the live console as captured
on 2026-09-12; visual chrome is reduced. All data is invented; no real PII.

Ground truth is exposed for tests ONLY through ``/__state`` (read) and
``/__reset`` (reset). The verifier never touches those: it reads the same
member-record page an operator would.

Run with: python -m fixture.meridian --port 8471
"""

from __future__ import annotations

import argparse
import copy
import itertools
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from flask import Flask, jsonify, redirect, render_template_string, request, session, url_for
from werkzeug.wrappers.response import Response


@dataclass
class Share:
    share_id: str
    type: str
    balance: str
    status: str  # OPEN | HOLD


@dataclass
class Member:
    number: str
    name: str
    email: str
    phone: str
    address: str
    shares: list[Share] = field(default_factory=list)


def seed_members() -> dict[str, Member]:
    """The live console's five demo members, statuses as observed live
    (103001-S0001 is seeded on HOLD, which is what makes a transfer from it
    fail validation)."""
    return {
        "100234": Member(
            "100234", "Lovelace, Ada", "ada.lovelace@example.com", "555-0101",
            "10 Analytical Way, Springfield",
            [Share("100234-S0001", "Regular Shares", "$2,500.00", "OPEN"),
             Share("100234-S0070", "Share Draft (Checking)", "$1,240.55", "OPEN")],
        ),
        "100987": Member(
            "100987", "Turing, Alan", "alan.turing@example.com", "555-0102",
            "22 Enigma Road, Springfield",
            [Share("100987-S0001", "Regular Shares", "$50.00", "OPEN"),
             Share("100987-S0070", "Share Draft (Checking)", "$5.25", "OPEN")],
        ),
        "101555": Member(
            "101555", "Hopper, Grace", "grace.hopper@example.com", "555-0103",
            "7 Compiler Court, Springfield",
            [Share("101555-S0001", "Regular Shares", "$18,000.00", "OPEN"),
             Share("101555-CERT", "Certificate", "$25,000.00", "OPEN")],
        ),
        "102777": Member(
            "102777", "Hamilton, Margaret", "margaret.hamilton@example.com", "555-0104",
            "1 Apollo Drive, Springfield",
            [Share("102777-S0001", "Regular Shares", "$42,000.00", "OPEN"),
             Share("102777-MMKT", "Money Market", "$9,800.00", "OPEN")],
        ),
        "103001": Member(
            "103001", "Knuth, Donald", "donald.knuth@example.com", "555-0105",
            "3 Volume Lane, Springfield",
            [Share("103001-S0001", "Regular Shares", "$760.50", "HOLD")],
        ),
    }


OPERATORS: dict[str, tuple[str, str]] = {
    # operator -> (display name, role)
    "teller1": ("T. ELLER", "TELLER"),
    "super1": ("S. VISOR", "SUPERVISOR"),
}
PASSWORD = "password"  # demo credential, same as the live console's published one

STYLE = """
body { background-color:#c8c8c8; margin:0; }
body, td, th, font, input, select, textarea, button { font-family: Verdana, Arial, sans-serif; font-size: 11px; color:#000000; }
a { color:#0000aa; } h1 { font-size:13px; margin:0; }
.fld { border:1px solid #7f9db9; background:#ffffff; padding:1px 2px; font-size:11px; }
.btn { font-size:11px; padding:1px 8px; } .lbl { font-weight:bold; }
.err { color:#a00000; font-weight:bold; } .ok { color:#006600; font-weight:bold; }
.box { border:2px solid #003366; background:#eef2f7; }
"""

PAGE = """<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN" "http://www.w3.org/TR/html4/loose.dtd">
<html><head><meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1">
<title>{{ title }} - Meridian Core</title><style type="text/css">{{ style }}</style></head>
<body>
<table width="760" border="0" cellpadding="0" cellspacing="0" align="center" style="margin-top:14px;background:#ffffff;border:1px solid #003366;">
<tr><td bgcolor="#003366" style="padding:6px 8px;">
<font face="Verdana" size="2" color="#ffffff"><b>MERIDIAN CORE</b></font>
<font face="Verdana" size="1" color="#a9c2e0">&nbsp;&nbsp;Member Services Platform &nbsp; v4.2.1 (local fixture)</font>
</td></tr>
{% if operator %}
<tr><td bgcolor="#5a7ca6" style="padding:2px 6px;"><font face="Verdana" size="1" color="#ffffff">
<a href="/menu" style="color:#ffffff;text-decoration:none;"><b>Main Menu</b></a> &nbsp;&middot;&nbsp;
<a href="/members" style="color:#ffffff;text-decoration:none;">Member Inquiry</a> &nbsp;&middot;&nbsp;
<a href="/settings" style="color:#ffffff;text-decoration:none;">System Settings</a> &nbsp;&middot;&nbsp;
<a href="/signoff" style="color:#ffffff;text-decoration:none;">Sign Off</a>
</font></td></tr>
{% endif %}
<tr><td style="padding:12px 14px;">{{ body|safe }}</td></tr>
<tr><td bgcolor="#e4e4e4" style="border-top:1px solid #999999;padding:3px 8px;">
<font face="Verdana" size="1" color="#333333">{% if operator %}OPR {{ operator|upper }} &nbsp;|&nbsp; BR MAIN-001 &nbsp;|&nbsp; SID {{ sid }}{% else %}NOT SIGNED ON{% endif %}</font>
</td></tr>
<tr><td bgcolor="#003366" style="padding:3px 8px;"><font face="Verdana" size="1" color="#c9d8ea">
<b>F3</b>=Sign Off &nbsp; <b>F5</b>=Main Menu &nbsp; <b>F7</b>=Member Inquiry &nbsp; <b>F12</b>=Cancel</font></td></tr>
</table></body></html>"""

SIGNON_BODY = """<h1>OPERATOR SIGN ON</h1><br>
{% if error %}<font class="err">{{ error }}</font><br><br>{% endif %}
<form method="post" action="/signon">
<table border="0" cellpadding="3" cellspacing="0" class="box" style="padding:10px;">
<tr><td class="lbl" align="right">Operator ID:</td><td><input class="fld" type="text" name="operator" size="20" autocomplete="off"></td></tr>
<tr><td class="lbl" align="right">Password:</td><td><input class="fld" type="password" name="password" size="20" autocomplete="off"></td></tr>
<tr><td class="lbl" align="right">Branch:</td><td><select class="fld" name="branch"><option value="MAIN-001">MAIN-001 - Main Office</option><option value="WEST-014">WEST-014 - Westside</option></select></td></tr>
<tr><td></td><td><br><input class="btn" type="submit" value="Sign On"></td></tr>
</table></form><br>
<font size="1" color="#555555">Demo operators: <b>teller1</b> / password &nbsp;&middot;&nbsp; <b>super1</b> / password (supervisor)</font>"""

MENU_BODY = """<h1>MAIN MENU</h1>
<font size="1" color="#555555">Signed on as {{ display }} ({{ role }})</font><br><br>
<table border="0" cellpadding="2" cellspacing="0" class="box" style="padding:10px;" width="360">
<tr><td width="28" align="right" class="lbl">1.</td><td>&nbsp;<a href="/members">Member Inquiry / Selection</a></td></tr>
<tr><td width="28" align="right" class="lbl">5.</td><td>&nbsp;<a href="/members?next=hold">Place Account Hold</a></td></tr>
<tr><td width="28" align="right" class="lbl">9.</td><td>&nbsp;<a href="/signoff">Sign Off</a></td></tr>
</table>"""

SEARCH_BODY = """<h1>MEMBER INQUIRY / SELECTION</h1><br>
<form method="get" action="/members">
<table border="0" cellpadding="3" cellspacing="0" class="box" style="padding:10px;">
<tr><td class="lbl" align="right">Search by:</td><td><select class="fld" name="by">
<option value="number" {% if by == 'number' %}selected{% endif %}>Member Number</option>
<option value="name" {% if by == 'name' %}selected{% endif %}>Last Name</option></select></td></tr>
<tr><td class="lbl" align="right">Value:</td><td><input class="fld" type="text" name="q" size="28" value="{{ q }}"> &nbsp; <input class="btn" type="submit" value="Search"></td></tr>
</table></form>
{% if searched %}<br>
{% if results %}
<table border="1" cellpadding="4" cellspacing="0" bordercolor="#7f9db9" width="100%">
<tr bgcolor="#dbe4ef" class="lbl"><td>Member No.</td><td>Name</td><td>Shares</td><td>&nbsp;</td></tr>
{% for m in results %}<tr><td>{{ m.number }}</td><td>{{ m.name }}</td><td>{{ m.shares|length }}</td><td><a href="/members/{{ m.number }}">Select</a></td></tr>{% endfor %}
</table>
{% else %}<font class="err">No member records matched your search.</font>{% endif %}
{% endif %}
<br><font size="1" color="#555555">Try member numbers 100234, 100987, 101555, 102777, 103001.</font>"""

MEMBER_BODY = """<h1>MEMBER RECORD</h1><br>
<table border="0" cellpadding="2" cellspacing="0" width="100%">
<tr><td class="lbl" width="120">Member No.:</td><td>{{ m.number }}</td><td class="lbl" width="80">Name:</td><td>{{ m.name }}</td></tr>
<tr><td class="lbl">E-mail:</td><td>{{ m.email }}</td><td class="lbl">Phone:</td><td>{{ m.phone }}</td></tr>
<tr><td class="lbl" valign="top">Address:</td><td colspan="3">{{ m.address }}</td></tr>
</table><br>
<font class="lbl">SHARES / BALANCES</font>
<table border="1" cellpadding="4" cellspacing="0" bordercolor="#7f9db9" width="100%">
<tr bgcolor="#dbe4ef" class="lbl"><td>Share ID</td><td>Type</td><td align="right">Balance</td><td>Status</td></tr>
{% for s in m.shares %}<tr><td>{{ s.share_id }}</td><td>{{ s.type }}</td><td align="right">{{ s.balance }}</td><td>{{ s.status }}{% if s.status == 'HOLD' %} <font class="err">[HOLD]</font>{% endif %}</td></tr>{% endfor %}
</table><br>
<font class="lbl">ACTIONS</font><br>
<a href="/members/{{ m.number }}/transfer">Funds Transfer</a> &nbsp;&middot;&nbsp;
<a href="/members/{{ m.number }}/open-share">Open New Share</a> &nbsp;&middot;&nbsp;
<a href="/members/{{ m.number }}/update">Update Member Information</a> &nbsp;&middot;&nbsp;
<a href="/members/{{ m.number }}/hold">Place Account Hold</a>"""

HOLD_FORM_BODY = """<h1>PLACE ACCOUNT HOLD</h1>
<font size="1" color="#a00000"><b>RESTRICTED FUNCTION - SUPERVISOR OVERRIDE REQUIRED</b></font><br><br>
{% if not supervisor %}
<table border="0" cellpadding="6" cellspacing="0" class="box"><tr><td>
<font class="err">SUPERVISOR OVERRIDE REQUIRED</font><br>
Operator {{ operator|upper }} is not authorized to perform this function. A supervisor must complete the hold.
</td></tr></table><br><a href="/members/{{ m.number }}">Cancel</a>
{% else %}
<form method="post" action="/members/{{ m.number }}/hold/review">
<input type="hidden" name="_token" value="{{ token }}">
<table border="0" cellpadding="3" cellspacing="0" class="box" style="padding:10px;">
<tr><td class="lbl" align="right">Share:</td><td><select class="fld" name="share">
{% for s in m.shares %}<option value="{{ s.share_id }}">{{ s.share_id }} - {{ s.type }}</option>{% endfor %}</select></td></tr>
<tr><td class="lbl" align="right">Reason Code:</td><td><select class="fld" name="reason">
<option value="FRAUD">FRAUD - Suspected fraud</option><option value="LEGAL">LEGAL - Legal / levy</option><option value="DECEASED">DECEASED - Member deceased</option></select></td></tr>
<tr><td class="lbl" align="right">Notes:</td><td><input class="fld" type="text" name="notes" size="48" maxlength="80"></td></tr>
<tr><td></td><td><br><input class="btn" type="submit" value="Continue"> &nbsp;<a href="/members/{{ m.number }}">Cancel</a></td></tr>
</table></form>
{% endif %}"""

HOLD_REVIEW_BODY = """<h1>CONFIRM ACCOUNT HOLD</h1><br>
<div class="box" style="padding:12px;"><font class="err" size="2">IRREVERSIBLE ACTION</font><br><br>
<table border="0" cellpadding="3" cellspacing="0">
<tr><td class="lbl" align="right">Member:</td><td>{{ m.number }} - {{ m.name }}</td></tr>
<tr><td class="lbl" align="right">Share:</td><td>{{ share.share_id }} - {{ share.type }}</td></tr>
<tr><td class="lbl" align="right">Reason:</td><td>{{ reason }}</td></tr>
<tr><td class="lbl" align="right">Notes:</td><td>{{ notes }}</td></tr>
</table></div><br>
<form method="post" action="/members/{{ m.number }}/hold/post">
<input type="hidden" name="_token" value="{{ token }}">
<input type="hidden" name="share" value="{{ share.share_id }}">
<input type="hidden" name="reason" value="{{ reason }}">
<input type="hidden" name="notes" value="{{ notes }}">
<input class="btn" type="submit" value="Apply Hold"> &nbsp;<a href="/members/{{ m.number }}">Cancel</a>
</form>"""

HOLD_APPLIED_BODY = """<h1>ACCOUNT HOLD APPLIED</h1><br>
<div class="box" style="padding:12px;"><font class="ok" size="2">HOLD RECORDED</font><br><br>
<table border="0" cellpadding="3" cellspacing="0">
<tr><td class="lbl" align="right">Confirmation:</td><td><b>{{ cn }}</b></td></tr>
<tr><td class="lbl" align="right">Share:</td><td>{{ share_id }} is now <b>HOLD</b></td></tr>
<tr><td class="lbl" align="right">Applied:</td><td>{{ applied }}</td></tr>
</table></div><br>
<a href="/members/{{ member }}">Return to Member Record</a> &nbsp;&middot;&nbsp; <a href="/menu">Main Menu</a>"""

VALIDATION_BODY = """<h1>{{ heading }}</h1><br>
<table border="0" cellpadding="6" cellspacing="0" class="box"><tr><td>
<font class="err">The request could not be validated:</font> {{ detail }}
</td></tr></table><br><a href="/members/{{ member }}">Return to Member Record</a>"""


def create_app() -> Flask:
    """Build the fixture app. State lives in the app object so a test can
    reset it between runs; each process starts from the seed."""
    app = Flask("meridian-fixture")
    app.secret_key = "local-fixture-not-a-secret"
    state: dict[str, Any] = {
        "members": seed_members(),
        "cn": itertools.count(480001),
        "holds": [],  # the fixture's own journal of applied holds (ground truth)
    }

    def render(title: str, body_tpl: str, **ctx: Any) -> str:
        operator = session.get("operator")
        body = render_template_string(body_tpl, operator=operator, **ctx)
        return render_template_string(
            PAGE, title=title, style=STYLE, body=body, operator=operator,
            sid=session.get("sid", ""),
        )

    def require_operator() -> Response | None:
        if not session.get("operator"):
            return redirect(url_for("signon_get"))
        return None

    def members() -> dict[str, Member]:
        result: dict[str, Member] = state["members"]
        return result

    @app.get("/")
    def root() -> Response:
        return redirect(url_for("signon_get"))

    @app.get("/signon")
    def signon_get() -> str:
        return render("Sign On", SIGNON_BODY, error=None)

    @app.post("/signon")
    def signon_post() -> str | Response:
        operator = request.form.get("operator", "").strip().lower()
        password = request.form.get("password", "")
        if operator in OPERATORS and password == PASSWORD:
            session["operator"] = operator
            session["sid"] = secrets.token_hex(4).upper()
            return redirect(url_for("menu"))
        return render("Sign On", SIGNON_BODY, error="Invalid operator ID or password.")

    @app.get("/signoff")
    def signoff() -> Response:
        session.clear()
        return redirect(url_for("signon_get"))

    @app.get("/menu")
    def menu() -> str | Response:
        if (r := require_operator()) is not None:
            return r
        display, role = OPERATORS[session["operator"]]
        return render("Main Menu", MENU_BODY, display=display, role=role)

    @app.get("/settings")
    def settings() -> str | Response:
        if (r := require_operator()) is not None:
            return r
        return render("System Settings", "<h1>SYSTEM SETTINGS</h1><br>No settings in the fixture.")

    @app.get("/members")
    def members_search() -> str | Response:
        if (r := require_operator()) is not None:
            return r
        by = request.args.get("by", "number")
        q = request.args.get("q", "").strip()
        searched = "q" in request.args
        results: list[Member] = []
        if searched and q:
            if by == "name":
                results = [m for m in members().values()
                           if m.name.split(",")[0].strip().lower() == q.lower()]
            else:
                results = [m for m in members().values() if m.number == q]
        return render("Member Inquiry", SEARCH_BODY, by=by, q=q, searched=searched,
                      results=results)

    @app.get("/members/<number>")
    def member_record(number: str) -> str | Response:
        if (r := require_operator()) is not None:
            return r
        m = members().get(number)
        if m is None:
            return redirect(url_for("members_search", by="number", q=number))
        return render("Member Record", MEMBER_BODY, m=m)

    def _is_supervisor() -> bool:
        return OPERATORS[session["operator"]][1] == "SUPERVISOR"

    @app.get("/members/<number>/hold")
    def hold_form(number: str) -> str | Response:
        if (r := require_operator()) is not None:
            return r
        m = members().get(number)
        if m is None:
            return redirect(url_for("members_search", by="number", q=number))
        token = session.setdefault("token", secrets.token_hex(6))
        return render("Place Account Hold", HOLD_FORM_BODY, m=m, token=token,
                      supervisor=_is_supervisor())

    @app.post("/members/<number>/hold/review")
    def hold_review(number: str) -> str | Response:
        if (r := require_operator()) is not None:
            return r
        m = members().get(number)
        if m is None or not _is_supervisor():
            return redirect(url_for("member_record", number=number))
        share = next((s for s in m.shares if s.share_id == request.form.get("share")), None)
        if share is None:
            return render("Place Account Hold", VALIDATION_BODY, heading="PLACE ACCOUNT HOLD",
                          detail="unknown share.", member=number)
        return render("Confirm Account Hold", HOLD_REVIEW_BODY, m=m, share=share,
                      reason=request.form.get("reason", "FRAUD"),
                      notes=request.form.get("notes", ""), token=session.get("token", ""))

    @app.post("/members/<number>/hold/post")
    def hold_post(number: str) -> str | Response | tuple[str, int]:
        """The consequential action. State changes HERE and only here."""
        if (r := require_operator()) is not None:
            return r
        m = members().get(number)
        if m is None or not _is_supervisor():
            return redirect(url_for("member_record", number=number))
        if request.form.get("_token") != session.get("token"):
            return render("Place Account Hold", VALIDATION_BODY, heading="PLACE ACCOUNT HOLD",
                          detail="stale form token.", member=number), 400
        share = next((s for s in m.shares if s.share_id == request.form.get("share")), None)
        if share is None:
            return render("Place Account Hold", VALIDATION_BODY, heading="PLACE ACCOUNT HOLD",
                          detail="unknown share.", member=number)
        if share.status == "HOLD":
            return render("Place Account Hold", VALIDATION_BODY, heading="PLACE ACCOUNT HOLD",
                          detail="share is already on hold.", member=number)
        share.status = "HOLD"
        cn = f"CN{next(state['cn'])}"
        applied = datetime.now(tz=UTC).strftime("%m/%d/%Y %H:%M:%S")
        state["holds"].append({"cn": cn, "member": number, "share": share.share_id,
                               "reason": request.form.get("reason"), "applied": applied,
                               "operator": session["operator"]})
        return render("Hold Applied", HOLD_APPLIED_BODY, cn=cn, share_id=share.share_id,
                      applied=applied, member=number)

    # ---- test-only ground truth (never used by the verifier) -----------------

    @app.get("/__state")
    def state_get() -> Response:
        return jsonify({
            "shares": {s.share_id: s.status for m in members().values() for s in m.shares},
            "holds": copy.deepcopy(state["holds"]),
        })

    @app.post("/__reset")
    def state_reset() -> Response:
        state["members"] = seed_members()
        state["holds"] = []
        return jsonify({"reset": True})

    return app


def main() -> None:
    parser = argparse.ArgumentParser(prog="meridian-fixture",
                                     description="MERIDIAN-shaped local fixture")
    parser.add_argument("--port", type=int, default=8471)
    args = parser.parse_args()
    create_app().run(host="127.0.0.1", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
