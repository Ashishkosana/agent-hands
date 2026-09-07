"""Human-in-the-loop escalation: pause, cede control of the live session,
resume — with a single owner at every moment.

Control-transfer model:

- The token is ENGINE-OWNED in-memory state. The operator console runs on an
  HTTP thread in the same process and never touches Playwright (the sync API
  is thread-affine); it only posts transition REQUESTS into this hub.
- The engine polls the hub at every tick of its existing poll loops and at
  step boundaries; on a pending pause it parks and acknowledges. The console
  shows control as granted only after that acknowledgment — two drivers on
  one session is structurally impossible, not discouraged.
- State machine: AUTOMATION -> PAUSED -> {HUMAN | AUTOMATION (approve) |
  FAILED (deny / TTL)}; HUMAN -> {AUTOMATION (hand back) | ABORTED (reason) |
  RESOLVED (outcome code, note)}. The human can approve without taking over,
  abort a run they judge unsafe, or resolve it as a business outcome they
  established manually — every transition records the operator identity.

Manual control has two equivalent paths through the same token machinery:
in headed mode the operator drives the visible browser directly (their
actions are captured by listeners injected at context creation); the console
additionally offers a minimal command channel (click/type by role+name) that
the PARKED ENGINE THREAD executes on the operator's behalf — the "bare/mock
operator surface" that makes handoff drivable headless and testable, without
violating thread affinity. Typed values through that channel are captured as
masked lengths only, like any human keystroke.
"""

from __future__ import annotations

import html
import json
import threading
from dataclasses import dataclass, field
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


class ControlState(Enum):
    AUTOMATION = "automation"
    PAUSED = "paused"
    HUMAN = "human"


class Decision(Enum):
    NONE = "none"
    APPROVE = "approve"  # resume automation without takeover
    DENY = "deny"  # refuse this run's intent; fail closed, no act
    HANDBACK = "handback"  # human is done; resume via forward scan
    ABORT = "abort"
    RESOLVE = "resolve"  # human established a business outcome manually


@dataclass
class Intervention:
    capability: str
    version: int
    step_id: str | None
    intent: str | None
    reason: str
    params: dict[str, str]  # non-sensitive only; masked upstream
    expected: str
    remaining_steps: list[str]
    recent_events: list[dict[str, object]]
    screenshot_path: str | None
    # Intent-approval extras. Default kind keeps the existing failure-escalation
    # path unchanged; the intent gate parks the same hub with kind="intent_approval".
    kind: str = "escalation"
    intent_hash: str | None = None
    prompt: str | None = None
    run_id: str | None = None
    artifact_sha256: str | None = None


@dataclass
class HumanCommand:
    kind: str  # click | type
    role: str
    name: str
    text: str = ""


@dataclass
class EscalationHub:
    """Shared state between the engine thread and the console thread. All
    mutation happens under one lock; the engine is the only consumer of
    commands and the only side that ever touches the browser."""

    _lock: threading.Lock = field(default_factory=threading.Lock)
    state: ControlState = ControlState.AUTOMATION
    intervention: Intervention | None = None
    operator: str | None = None
    decision: Decision = Decision.NONE
    decision_note: str = ""
    resolve_code: str = ""
    pause_requested: bool = False
    take_requested: bool = False
    _commands: list[HumanCommand] = field(default_factory=list)
    human_actions: list[dict[str, object]] = field(default_factory=list)

    # ------------------------------------------------- console-side requests

    def request_control(self, operator: str) -> None:
        """Operator-initiated takeover: sets a flag the engine polls; the
        engine parks at its next tick. 'Can I stop it when I see it going
        wrong?' — yes, at poll-tick granularity."""
        with self._lock:
            self.pause_requested = True
            self.take_requested = True
            self.operator = operator

    def take(self, operator: str) -> bool:
        with self._lock:
            if self.state is not ControlState.PAUSED:
                return False
            self.state = ControlState.HUMAN
            self.operator = operator
            return True

    def submit_command(self, command: HumanCommand) -> bool:
        with self._lock:
            if self.state is not ControlState.HUMAN:
                return False
            self._commands.append(command)
            return True

    def decide(self, decision: Decision, operator: str, note: str = "", code: str = "") -> bool:
        with self._lock:
            needs_human = decision in (Decision.HANDBACK, Decision.ABORT, Decision.RESOLVE)
            if needs_human and self.state is not ControlState.HUMAN:
                return False
            if decision is Decision.APPROVE and self.state is not ControlState.PAUSED:
                return False
            if decision is Decision.DENY and self.state not in (
                ControlState.PAUSED,
                ControlState.HUMAN,
            ):
                return False
            self.decision = decision
            self.decision_note = note
            self.resolve_code = code
            self.operator = operator
            return True

    # --------------------------------------------------- engine-side control

    def park(self, intervention: Intervention) -> None:
        with self._lock:
            self.state = ControlState.PAUSED
            self.intervention = intervention
            self.decision = Decision.NONE
            self.pause_requested = False

    def pop_command(self) -> HumanCommand | None:
        with self._lock:
            return self._commands.pop(0) if self._commands else None

    def take_decision(self) -> tuple[Decision, str, str, str | None]:
        with self._lock:
            decision = self.decision
            self.decision = Decision.NONE
            return decision, self.decision_note, self.resolve_code, self.operator

    def record_human_action(self, event: dict[str, object]) -> bool:
        """Capture callback target. Records ONLY while a human holds control —
        the binding is installed for the whole session but stays dormant."""
        with self._lock:
            if self.state is not ControlState.HUMAN:
                return False
            self.human_actions.append(event)
            return True

    def resume_automation(self) -> None:
        with self._lock:
            self.state = ControlState.AUTOMATION
            self.intervention = None
            self.take_requested = False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self.state.value,
                "operator": self.operator,
                "intervention": self.intervention.__dict__ if self.intervention else None,
                "human_actions": list(self.human_actions),
            }


# --------------------------------------------------------------------- console


_PAGE = """<!doctype html><html><head><title>hands operator console</title>
<style>
 body {{ font: 14px/1.5 -apple-system, sans-serif; margin: 2rem; max-width: 720px; }}
 h1 {{ font-size: 1.1rem; }} pre {{ background: #f4f4f4; padding: .8rem; overflow-x: auto; }}
 .state {{ font-weight: 700; text-transform: uppercase; }}
 .prompt {{ font-size: 1.05rem; background: #fff8e1; padding: .8rem;
  border-left: 4px solid #f9a825; }}
 form {{ display: inline-block; margin: .3rem .4rem .3rem 0; }}
 input[type=submit] {{ padding: .35rem .8rem; }}
 input[type=text] {{ padding: .3rem; }}
</style><meta http-equiv="refresh" content="2"></head><body>
<h1>hands — operator console</h1>
<p>control: <span class="state">{state}</span> &middot; operator: {operator}</p>
<pre>{context}</pre>
{controls}
</body></html>"""

_PAUSED_CONTROLS = """
<form method="post" action="/take"><input type="text" name="operator" value="{op}">
<input type="submit" value="Take control"></form>
<form method="post" action="/approve"><input type="hidden" name="operator" value="{op}">
<input type="submit" value="Approve &amp; resume"></form>
"""

_INTENT_CONTROLS = """
<p class="prompt"><strong>{prompt}</strong></p>
<p>Approve binds your operator id and this run's <code>intent_hash</code> into
the hash-chained trace. Deny (or an unanswered TTL) fails closed — the
transfer is not posted. Identities are attestations, not cryptographic keys.</p>
<form method="post" action="/approve">
 <input type="text" name="operator" value="{op}" placeholder="operator id">
 <input type="submit" value="Approve transfer"></form>
<form method="post" action="/deny">
 <input type="hidden" name="operator" value="{op}">
 <input type="submit" value="Deny transfer"></form>
"""

_HUMAN_CONTROLS = """
<form method="post" action="/act">
 <input type="hidden" name="operator" value="{op}">
 kind <input type="text" name="kind" value="click" size="5">
 role <input type="text" name="role" value="button" size="8">
 name <input type="text" name="name" size="18">
 text <input type="text" name="text" size="12">
 <input type="submit" value="Do it"></form><br>
<form method="post" action="/handback"><input type="hidden" name="operator" value="{op}">
<input type="submit" value="Hand control back"></form>
<form method="post" action="/resolve"><input type="hidden" name="operator" value="{op}">
 code <input type="text" name="code" size="20"> note <input type="text" name="note" size="20">
<input type="submit" value="Resolve as outcome"></form>
<form method="post" action="/abort"><input type="hidden" name="operator" value="{op}">
 reason <input type="text" name="reason" size="24">
<input type="submit" value="Abort run"></form>
"""

_RUNNING_CONTROLS = """
<form method="post" action="/request-control"><input type="text" name="operator" value="{op}">
<input type="submit" value="Request control"></form>
"""


class ConsoleServer:
    """Minimal operator console. Runs on a daemon thread; talks only to the
    hub (and serves the intervention screenshot file). Never touches the
    browser."""

    def __init__(self, hub: EscalationHub, port: int = 0) -> None:
        self.hub = hub
        handler = self._make_handler()
        self._server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        hub = self.hub

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:  # silence
                del fmt, args

            def _respond(self, body: bytes, content_type: str = "text/html") -> None:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                snap = hub.snapshot()
                if path == "/state":
                    self._respond(json.dumps(snap, default=str).encode(), "application/json")
                    return
                if path == "/screenshot.png":
                    intervention = snap.get("intervention") or {}
                    shot = intervention.get("screenshot_path")
                    if shot and Path(shot).exists():
                        self._respond(Path(shot).read_bytes(), "image/png")
                        return
                    self.send_response(404)
                    self.end_headers()
                    return
                op = snap.get("operator") or "teller1"
                intervention = snap.get("intervention") or {}
                kind = intervention.get("kind") if isinstance(intervention, dict) else None
                if (
                    snap["state"] == ControlState.PAUSED.value
                    and kind == "intent_approval"
                ):
                    raw = intervention.get("prompt") or "Approve this transfer?"
                    prompt = html.escape(str(raw))
                    controls = _INTENT_CONTROLS.format(op=html.escape(str(op)), prompt=prompt)
                elif snap["state"] == ControlState.PAUSED.value:
                    controls = _PAUSED_CONTROLS.format(op=op)
                elif snap["state"] == ControlState.HUMAN.value:
                    controls = _HUMAN_CONTROLS.format(op=op)
                else:
                    controls = _RUNNING_CONTROLS.format(op=op)
                body = _PAGE.format(
                    state=snap["state"],
                    operator=op,
                    context=json.dumps(snap.get("intervention"), indent=2, default=str),
                    controls=controls,
                )
                self._respond(body.encode())

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                form = parse_qs(self.rfile.read(length).decode())

                def get(key: str, default: str = "") -> str:
                    values = form.get(key) or [default]
                    return values[0] or default

                operator = get("operator", "operator")
                path = urlparse(self.path).path
                ok = False
                if path == "/request-control":
                    hub.request_control(operator)
                    ok = True
                elif path == "/take":
                    ok = hub.take(operator)
                elif path == "/act":
                    ok = hub.submit_command(
                        HumanCommand(
                            kind=get("kind", "click"),
                            role=get("role", "button"),
                            name=get("name"),
                            text=get("text"),
                        )
                    )
                elif path == "/approve":
                    ok = hub.decide(Decision.APPROVE, operator)
                elif path == "/deny":
                    ok = hub.decide(Decision.DENY, operator, note=get("reason"))
                elif path == "/handback":
                    ok = hub.decide(Decision.HANDBACK, operator)
                elif path == "/abort":
                    ok = hub.decide(Decision.ABORT, operator, note=get("reason"))
                elif path == "/resolve":
                    ok = hub.decide(Decision.RESOLVE, operator, note=get("note"), code=get("code"))
                self.send_response(303 if ok else 409)
                self.send_header("Location", "/")
                self.end_headers()

        return Handler
