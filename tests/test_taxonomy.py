"""The failure taxonomy, exercised against injected runtime states.

Each scenario asserts the CORRECT CLASSIFICATION, not merely non-crash — the
three-way contract (answer / recovered / loud failure) is the load-bearing
claim, and conflating its classes is the domain's classic design mistake.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from hands.artifact import Capability
from hands.conditions import RecognizerHit
from hands.replay import EngineConfig, ReplayEngine, _Recover
from hands.results import BusinessOutcome, Failure, MatchEvidence, Success
from tests.conftest import set_fault


def engine(tmp_path: Path) -> ReplayEngine:
    return ReplayEngine(EngineConfig(runs_dir=tmp_path / "runs"))


def trace_events(tmp_path: Path) -> list[dict[str, object]]:
    (run_dir,) = (tmp_path / "runs").iterdir()
    return [
        json.loads(line) for line in (run_dir / "trace.jsonl").read_text().splitlines()
    ]


# --------------------------------------------------------- business outcomes


def test_server_side_validation_is_an_answer(capability: Capability, tmp_path: Path) -> None:
    """The client pattern lets 'abc' through; the SERVER rejects it. That
    rejection is a legitimate business outcome the caller needs."""
    result = engine(tmp_path).run(capability, {"member_id": "abc"})
    assert isinstance(result, BusinessOutcome)
    assert result.code == "VALIDATION_REJECTED"
    assert "must be numeric" in result.evidence.matched_text


def test_permission_denied_is_an_answer(
    capability: Capability, fixture_app: str, tmp_path: Path
) -> None:
    set_fault(fixture_app, "permission_denied", True)
    result = engine(tmp_path).run(capability, {"member_id": "12345"})
    assert isinstance(result, BusinessOutcome)
    assert result.code == "PERMISSION_DENIED"


# --------------------------------------------------------- recoverable states


def test_session_expiry_is_recovered_invisibly(
    capability: Capability, fixture_app: str, tmp_path: Path
) -> None:
    """The session-expired interstitial intercepts the entry page; the engine
    clicks Continue Session, verifies the state is GONE, retries the step,
    and the run still SUCCEEDS — recovered, logged, never surfaced."""
    set_fault(fixture_app, "session_expired", True)
    result = engine(tmp_path).run(capability, {"member_id": "12345"})
    assert isinstance(result, Success)
    assert result.outputs == {"savings_balance": Decimal("1234.50")}

    events = [e["event"] for e in trace_events(tmp_path)]
    assert "recovery_step_started" in events
    assert "recovery_step_done" in events
    fired = [e for e in trace_events(tmp_path) if e["event"] == "recognizer_fired"]
    assert any(e.get("classify") == "recoverable" for e in fired)


def test_interstitial_is_dismissed_and_run_succeeds(
    capability: Capability, fixture_app: str, tmp_path: Path
) -> None:
    set_fault(fixture_app, "interstitial", True)
    result = engine(tmp_path).run(capability, {"member_id": "12345"})
    assert isinstance(result, Success)


def test_transient_slow_load_is_absorbed_by_the_wait_model(
    capability: Capability, fixture_app: str, tmp_path: Path
) -> None:
    set_fault(fixture_app, "slow_load", True)
    result = engine(tmp_path).run(capability, {"member_id": "12345"})
    assert isinstance(result, Success)


# ------------------------------------------------------------- hard failures


def test_app_error_is_a_loud_debuggable_failure(
    capability: Capability, fixture_app: str, tmp_path: Path
) -> None:
    """A 500 page is an UNRECOGNIZED blocking state: no recognizer matches,
    the postcondition times out, and the report says exactly what was
    expected and what was observed — with evidence attached."""
    set_fault(fixture_app, "app_error", True)
    result = engine(tmp_path).run(capability, {"member_id": "12345"})
    assert isinstance(result, Failure)
    assert result.report.step_id == "s2"
    assert "Member Details" in result.report.expected
    assert result.report.screenshot_path is not None
    assert Path(result.report.screenshot_path).exists()


def test_recovery_cap_exhaustion_promotes_to_failure(
    capability: Capability, fixture_app: str, tmp_path: Path
) -> None:
    """A recoverable condition is bounded: when its per-run fire cap is
    exhausted, the run fails loudly NAMING the condition — recoverable never
    silently becomes an infinite loop or a fourth result class."""
    for condition in capability.conditions:
        if condition.id == "session_expired":
            condition.max_fires_per_run = 0
    set_fault(fixture_app, "session_expired", True)
    result = engine(tmp_path).run(capability, {"member_id": "12345"})
    assert isinstance(result, Failure)
    assert "session_expired" in result.report.observed
    assert "cap exhausted" in result.report.observed


def test_broken_recovery_promotes_to_failure_naming_the_condition(
    capability: Capability, fixture_app: str, tmp_path: Path
) -> None:
    """If the recovery itself cannot execute (the Continue button is gone),
    the run fails hard and the report names the original condition."""
    for condition in capability.conditions:
        if condition.id == "session_expired":
            target = condition.recovery[0].target
            assert target is not None
            target.ladder[0].name = "Proceed Anyway"  # type: ignore[union-attr]
    set_fault(fixture_app, "session_expired", True)
    result = engine(tmp_path).run(capability, {"member_id": "12345"})
    assert isinstance(result, Failure)
    assert result.report.step_id == "rs1"
    assert "session_expired" in (result.report.intent or "")


def test_risky_step_is_never_retried_after_its_action_ran(
    capability: Capability, tmp_path: Path
) -> None:
    """The double-fire prohibition, unit-level: a recoverable condition that
    interrupts a risky step AFTER its action ran must fail to escalation,
    never auto-retry. (Deterministic white-box check: the interrupt timing
    cannot be injected reliably through the live app.)"""
    risky_step = capability.steps[1]
    risky_step.risk = "risky"
    condition = next(c for c in capability.conditions if c.id == "session_expired")
    hit = RecognizerHit(
        condition=condition,
        evidence=MatchEvidence(condition_id=condition.id, matched_text="Session Expired"),
    )
    from hands.replay import _StepFailed

    eng = engine(tmp_path)
    with pytest.raises(_StepFailed) as failure:
        eng._recover(
            capability,
            {"member_id": "12345"},
            surface=None,  # type: ignore[arg-type]  # must fail before any surface use
            trace=_NullTrace(),  # type: ignore[arg-type]
            rec=_Recover(hit, acted=True),
            fires={},
            step_order={s.id: i for i, s in enumerate(capability.steps)},
            current=1,
            step=risky_step,
        )
    assert "never auto-retried" in failure.value.report.expected


class _NullTrace:
    run_dir = Path(".")

    def emit(self, event: str, **fields: object) -> None:
        pass


def test_ui_drift_fails_loud_never_guesses(
    capability: Capability, fixture_app: str, tmp_path: Path
) -> None:
    """Secondary per the brief: UI drift. A renamed label exhausts every
    ladder rung and the run fails LOUDLY, listing what was tried — in a
    stable-UI environment the drift answer is detect + fail-safe + telemetry,
    never a guessed click."""
    set_fault(fixture_app, "renamed_label", True)
    result = engine(tmp_path).run(capability, {"member_id": "12345"})
    assert isinstance(result, Failure)
    assert result.report.step_id == "s1"
    assert "no rung matched" in result.report.observed
    assert "Member ID" in result.report.observed  # what it tried, human-readable
