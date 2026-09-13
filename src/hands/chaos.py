"""Test/eval-only fault injection at the network seam.

A ``FaultInjector`` is a ``RequestInterceptor`` (see hands.surface) installed
on ONE surface — the executor's or the verifier's — through
``EngineConfig.interceptor``. It is never reachable from the CLI or the API.

The discipline that makes the experiment honest:

- Faults act on the RESPONSE the browser sees, or on whether the request is
  forwarded at all. They never write to the target system. In
  COMMIT_WITH_LOST_ACK the real request is forwarded and the real server
  commits; only the acknowledgement is withheld. In NO_COMMIT_WITH_LOST_ACK
  the request is dropped before it leaves. In FALSE_SUCCESS the request is
  dropped and a success-looking page is fabricated — the PRESENTATION is
  faked, never the durable state.
- The verifier reads the same durable state an operator would. Nothing in
  this module can influence what the verifier sees except VERIFIER_FAILURE,
  which breaks the verifier's own read path (its report must then be
  UNVERIFIABLE, never a guess).
- Everything an injector did is recorded in ``fired`` for the evidence
  bundle, and is NOT an input to the reconciler.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import parse_qs

from playwright.sync_api import Request as PwRequest
from playwright.sync_api import Route


class ChaosMode(StrEnum):
    COMMIT_WITH_LOST_ACK = "COMMIT_WITH_LOST_ACK"
    NO_COMMIT_WITH_LOST_ACK = "NO_COMMIT_WITH_LOST_ACK"
    FALSE_SUCCESS = "FALSE_SUCCESS"
    VERIFIER_FAILURE = "VERIFIER_FAILURE"


# The consequential request of the hold flow, as the live console shapes it.
HOLD_POST_RE = re.compile(r"/members/(?P<member>\d+)/hold/post$")
# The verifier's read of durable state.
MEMBER_RECORD_RE = re.compile(r"/members/(?P<member>\d+)$")


def fake_hold_applied_page(member: str, share_id: str) -> str:
    """A success-looking ACCOUNT HOLD APPLIED page for FALSE_SUCCESS. It
    satisfies the executor artifact's postcondition (the heading), checkpoint
    (identity: the share id carries the member number), and output
    (a confirmation number). None of it happened."""
    member = html.escape(member)
    share_id = html.escape(share_id)
    return (
        "<html><head><title>Hold Applied - Meridian Core</title></head><body>"
        "<table width='760'><tr><td><h1>ACCOUNT HOLD APPLIED</h1><br>"
        "<div><font size='2'>HOLD RECORDED</font><br><br>"
        "<table border='0' cellpadding='3' cellspacing='0'>"
        "<tr><td align='right'>Confirmation:</td><td><b>CN000000</b></td></tr>"
        f"<tr><td align='right'>Share:</td><td>{share_id} is now <b>HOLD</b></td></tr>"
        "<tr><td align='right'>Applied:</td><td>--</td></tr>"
        "</table></div><br>"
        f"<a href='/members/{member}'>Return to Member Record</a>"
        "</td></tr></table></body></html>"
    )


def maintenance_page() -> str:
    return (
        "<html><head><title>Maintenance - Meridian Core</title></head><body>"
        "<table width='760'><tr><td><h1>SYSTEM UNAVAILABLE</h1><br>"
        "<font>MERIDIAN CORE is temporarily unavailable. Please try again later.</font>"
        "</td></tr></table></body></html>"
    )


@dataclass
class FaultInjector:
    """Callable RequestInterceptor for one chaos mode.

    ``target`` matches the request the fault applies to (defaults per mode);
    ``once`` fires the fault a single time so the rest of the flow behaves
    normally — a lost ack is a one-shot event. Default: once for the
    executor-side modes; persistent for VERIFIER_FAILURE (an outage is not a
    single dropped packet).
    """

    mode: ChaosMode
    target: re.Pattern[str] | None = None
    once: bool | None = None
    fired: list[dict[str, object]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.target is None:
            self.target = (
                MEMBER_RECORD_RE if self.mode is ChaosMode.VERIFIER_FAILURE else HOLD_POST_RE
            )
        if self.once is None:
            self.once = self.mode is not ChaosMode.VERIFIER_FAILURE

    def _applies(self, request: PwRequest) -> re.Match[str] | None:
        if self.once and self.fired:
            return None
        assert self.target is not None
        if self.mode is ChaosMode.VERIFIER_FAILURE:
            if request.method.upper() != "GET":
                return None
        elif request.method.upper() != "POST":
            return None
        return self.target.search(request.url.split("?", 1)[0])

    def __call__(self, route: Route, request: PwRequest) -> bool:
        match = self._applies(request)
        if match is None:
            return False
        record: dict[str, object] = {"mode": self.mode.value, "url": request.url,
                                     "method": request.method.upper()}
        if self.mode is ChaosMode.COMMIT_WITH_LOST_ACK:
            # Forward the real request; the real server commits (or not — that
            # is ITS decision). Then withhold the answer from the browser.
            response = route.fetch()
            record["server_status"] = response.status
            record["response_withheld"] = True
            route.abort("connectionreset")
        elif self.mode is ChaosMode.NO_COMMIT_WITH_LOST_ACK:
            record["forwarded"] = False
            route.abort("connectionfailed")
        elif self.mode is ChaosMode.FALSE_SUCCESS:
            form = parse_qs(request.post_data or "")
            share_id = (form.get("share") or ["?"])[0]
            record["forwarded"] = False
            record["fabricated"] = "ACCOUNT HOLD APPLIED"
            route.fulfill(
                status=200,
                content_type="text/html; charset=iso-8859-1",
                body=fake_hold_applied_page(match.group("member"), share_id),
            )
        else:  # VERIFIER_FAILURE: the read path is down
            record["forwarded"] = False
            route.fulfill(status=503, content_type="text/html", body=maintenance_page())
        self.fired.append(record)
        return True
