"""Human-in-the-loop escalation, end to end against the live fixture.

The "operator" here is a thread speaking to the real console over HTTP —
exactly what a human's browser would do — while the engine owns the browser.
The engine thread is the only thread that ever touches Playwright; the
console's mock manual-control channel exists precisely so a headless test
(or a headless deployment) can drive the SAME live session through the same
control-token machinery a headed human would use.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from decimal import Decimal
from pathlib import Path

from hands.artifact import Capability
from hands.replay import EngineConfig, EscalationSettings, ReplayEngine
from hands.results import BusinessOutcome, Failure, Success
from tests.conftest import set_fault


def attended_engine(tmp_path: Path, ttl_s: float = 30.0) -> ReplayEngine:
    return ReplayEngine(
        EngineConfig(
            runs_dir=tmp_path / "runs",
            escalation=EscalationSettings(ttl_s=ttl_s, console_port=0),
        )
    )


def strip_recovery(capability: Capability, condition_id: str) -> None:
    """Remove a recoverable condition so its state becomes an unrecognized
    blocking state — the classic escalation trigger."""
    capability.conditions = [c for c in capability.conditions if c.id != condition_id]


class Operator:
    """A scripted operator: polls the run's trace directory to find the
    console URL, then interacts with it over HTTP like a person would."""

    def __init__(self, runs_dir: Path, script: list[dict[str, str]]) -> None:
        self.runs_dir = runs_dir
        self.script = script
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.error: str | None = None

    def start(self) -> None:
        self.thread.start()

    def join(self) -> None:
        self.thread.join(timeout=60)
        assert self.error is None, self.error

    def _console_url(self) -> str:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            for run_dir in self.runs_dir.glob("*"):
                trace = run_dir / "trace.jsonl"
                if not trace.exists():
                    continue
                for line in trace.read_text().splitlines():
                    event = json.loads(line)
                    if event["event"] == "operator_console_started":
                        return str(event["url"])
            time.sleep(0.1)
        raise AssertionError("console never started")

    def _await_event(self, event: str) -> None:
        """Wait for the ENGINE to record that it did something, rather than
        sleeping and hoping. A fixed sleep is not a synchronization point: hand
        back before the operator's click has actually executed and the engine
        resume-scans into the same blocking state, raises a SECOND intervention
        nobody is scripted to answer, and the run dies on the TTL instead."""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            for run_dir in self.runs_dir.glob("*"):
                trace = run_dir / "trace.jsonl"
                if not trace.exists():
                    continue
                for line in trace.read_text().splitlines():
                    if not line.strip():
                        continue
                    try:
                        if json.loads(line).get("event") == event:
                            return
                    except json.JSONDecodeError:
                        continue  # a line still being written
            time.sleep(0.05)
        raise AssertionError(f"engine never emitted {event!r}")

    def _await_state(self, url: str, state: str) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            with urllib.request.urlopen(url + "state", timeout=5) as response:
                snapshot = json.loads(response.read())
            if snapshot["state"] == state:
                return
            time.sleep(0.1)
        raise AssertionError(f"console never reached state {state!r}")

    def _post(self, url: str, path: str, **fields: str) -> None:
        body = urllib.parse.urlencode({"operator": "teller7", **fields}).encode()
        request = urllib.request.Request(url + path.lstrip("/"), data=body)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                response.read()
        except urllib.error.HTTPError as error:  # 303 redirect is success
            if error.code not in (303,):
                raise

    def _run(self) -> None:
        try:
            url = self._console_url()
            for action in self.script:
                kind = action.pop("do")
                if kind == "await":
                    self._await_state(url, action.pop("state"))
                elif kind == "await-event":
                    self._await_event(action.pop("event"))
                elif kind == "sleep":
                    time.sleep(float(action.pop("seconds")))
                else:
                    self._post(url, kind, **action)
        except Exception as exc:
            self.error = f"operator thread failed: {exc}"


def test_human_fixes_blocking_state_and_hands_back(
    capability: Capability, fixture_app: str, tmp_path: Path
) -> None:
    """The full handoff: unrecognized blocking state -> intervention -> human
    takes the SAME live session, clicks Continue Session through the mock
    control channel, hands back -> the engine resume-scans, retries, and the
    run SUCCEEDS."""
    strip_recovery(capability, "session_expired")
    set_fault(fixture_app, "session_expired", True)
    operator = Operator(
        tmp_path / "runs",
        [
            {"do": "await", "state": "paused"},
            {"do": "take"},
            {"do": "act", "kind": "click", "role": "button", "name": "Continue Session"},
            {"do": "await-event", "event": "human_command_executed"},
            {"do": "handback"},
        ],
    )
    operator.start()
    result = attended_engine(tmp_path).run(capability, {"member_id": "12345"})
    operator.join()
    assert isinstance(result, Success)
    assert result.outputs == {"savings_balance": Decimal("1234.50")}

    events = _events(tmp_path)
    kinds = [e["event"] for e in events]
    for expected in (
        "escalation_raised",
        "control_granted",
        "human_command_executed",
        "human_action",  # the injected capture saw the click
        "resume_decision",
    ):
        assert expected in kinds, f"missing {expected} in trace"
    # The intervention request carried actionable context.
    (intervention_file,) = (tmp_path / "runs").glob("*/intervention-1.json")
    intervention = json.loads(intervention_file.read_text())
    assert intervention["capability"] == capability.name
    assert intervention["remaining_steps"]
    assert intervention["recent_events"]


def test_typed_sentinel_never_persists(
    capability: Capability, fixture_app: str, tmp_path: Path
) -> None:
    """The redaction canary: a 'human' types a sentinel during handoff; the
    sentinel must appear NOWHERE under the run directory — capture records
    masked lengths, never values."""
    sentinel = "HUNTER2-SENTINEL-9931"
    strip_recovery(capability, "session_expired")
    set_fault(fixture_app, "session_expired", True)
    operator = Operator(
        tmp_path / "runs",
        [
            {"do": "await", "state": "paused"},
            {"do": "take"},
            {"do": "act", "kind": "click", "role": "button", "name": "Continue Session"},
            {"do": "sleep", "seconds": "0.5"},
            {"do": "act", "kind": "type", "role": "textbox", "name": "", "text": sentinel},
            {"do": "sleep", "seconds": "0.5"},
            {"do": "abort", "reason": "canary test done"},
        ],
    )
    operator.start()
    result = attended_engine(tmp_path).run(capability, {"member_id": "12345"})
    operator.join()
    assert isinstance(result, Failure)  # aborted by the operator

    for path in (tmp_path / "runs").rglob("*"):
        if path.is_file() and path.suffix in (".jsonl", ".json", ".txt"):
            assert sentinel not in path.read_text(), f"sentinel leaked into {path.name}"


def test_operator_can_resolve_as_business_outcome(
    capability: Capability, fixture_app: str, tmp_path: Path
) -> None:
    """The human establishes the answer manually and resolves the run as a
    declared business outcome — attributed, not fabricated: an undeclared
    code is refused and the run stays parked."""
    strip_recovery(capability, "session_expired")
    set_fault(fixture_app, "session_expired", True)
    operator = Operator(
        tmp_path / "runs",
        [
            {"do": "await", "state": "paused"},
            {"do": "take"},
            {"do": "resolve", "code": "NOT_A_DECLARED_CODE", "note": "should be refused"},
            {"do": "sleep", "seconds": "0.5"},
            {"do": "resolve", "code": "MEMBER_NOT_FOUND", "note": "checked the core directly"},
        ],
    )
    operator.start()
    result = attended_engine(tmp_path).run(capability, {"member_id": "12345"})
    operator.join()
    assert isinstance(result, BusinessOutcome)
    assert result.code == "MEMBER_NOT_FOUND"
    assert result.evidence.condition_id == "human:teller7"
    events = [e["event"] for e in _events(tmp_path)]
    assert "escalation_invalid_decision" in events


def test_unanswered_intervention_expires(
    capability: Capability, fixture_app: str, tmp_path: Path
) -> None:
    """Nobody answers: the TTL closes the run as a loud failure — a banking
    session is never kept alive waiting for a human who isn't coming."""
    strip_recovery(capability, "session_expired")
    set_fault(fixture_app, "session_expired", True)
    result = attended_engine(tmp_path, ttl_s=1.0).run(capability, {"member_id": "12345"})
    assert isinstance(result, Failure)
    assert "unanswered" in result.report.observed
    assert "escalation_ttl_expired" in [e["event"] for e in _events(tmp_path)]


def test_operator_initiated_takeover_mid_run(
    capability: Capability, fixture_app: str, tmp_path: Path
) -> None:
    """'Can I stop it when I see it going wrong?' — the operator requests
    control during a healthy run; the engine parks at the next poll tick;
    approve resumes it and the run still succeeds."""
    set_fault(fixture_app, "slow_load", True)  # slow page = a wait to interrupt
    operator = Operator(
        tmp_path / "runs",
        [
            {"do": "request-control"},
            {"do": "await", "state": "paused"},
            {"do": "approve"},
        ],
    )
    operator.start()
    result = attended_engine(tmp_path).run(capability, {"member_id": "12345"})
    operator.join()
    assert isinstance(result, Success)
    events = [e["event"] for e in _events(tmp_path)]
    assert "escalation_raised" in events
    assert "resume_decision" in events


def _events(tmp_path: Path) -> list[dict[str, object]]:
    (run_dir,) = (tmp_path / "runs").iterdir()
    return [
        json.loads(line) for line in (run_dir / "trace.jsonl").read_text().splitlines()
    ]
