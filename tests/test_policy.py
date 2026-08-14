"""Policy guardrails: allowlist enforcement and the unattended-risky gate.

These are negative tests by design — the interesting assertion is what the
system REFUSES to do.
"""

from __future__ import annotations

from pathlib import Path

from hands.artifact import (
    Capability,
    NavigateAction,
    Step,
    UrlMatches,
    risk_review_valid,
    sign_risk_review,
)
from hands.replay import EngineConfig, ReplayEngine
from hands.results import PolicyViolation, Success


def engine(tmp_path: Path) -> ReplayEngine:
    return ReplayEngine(EngineConfig(runs_dir=tmp_path / "runs"))


def test_off_allowlist_navigation_is_blocked_and_reported(
    capability: Capability, tmp_path: Path
) -> None:
    """A step that tries to leave the allowlist is stopped at the NETWORK
    layer and the run reports a policy violation — not a mysterious timeout."""
    capability.steps.insert(
        0,
        Step(
            id="s0",
            intent="wander off to an external site",
            action=NavigateAction(url="https://example.com/definitely-not-allowed"),
            target=None,
            pre=[],
            post=[UrlMatches(pattern="example")],
            risk="safe",
        ),
    )
    result = engine(tmp_path).run(capability, {"member_id": "12345"})
    assert isinstance(result, PolicyViolation)
    assert result.rule == "off_allowlist_traffic"
    assert "example.com" in result.detail


def test_unattended_risky_replay_requires_signed_review(
    capability: Capability, tmp_path: Path
) -> None:
    """Mutating-by-default with teeth: risky steps do not replay unattended
    until a human signed the artifact. No browser is even launched."""
    capability.steps[1].risk = "risky"
    result = engine(tmp_path).run(capability, {"member_id": "12345"})
    assert isinstance(result, PolicyViolation)
    assert result.rule == "unattended_risky_requires_review"


def test_signed_review_unlocks_unattended_replay(
    capability: Capability, tmp_path: Path
) -> None:
    capability.steps[1].risk = "risky"
    signed = sign_risk_review(capability, "reviewer-1")
    assert risk_review_valid(signed)
    result = engine(tmp_path).run(signed, {"member_id": "12345"})
    assert isinstance(result, Success)


def test_any_artifact_change_invalidates_the_signature(
    capability: Capability, tmp_path: Path
) -> None:
    """The signature binds to the artifact hash: re-record or hand-edit the
    steps and prior approval is void — re-review is structural, not process."""
    capability.steps[1].risk = "risky"
    signed = sign_risk_review(capability, "reviewer-1")
    signed.steps[0].intent = "changed after review"
    assert not risk_review_valid(signed)
    result = engine(tmp_path).run(signed, {"member_id": "12345"})
    assert isinstance(result, PolicyViolation)
