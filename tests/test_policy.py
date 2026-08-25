"""Policy guardrails: allowlist enforcement and the unattended-risky gate.

These are negative tests by design — the interesting assertion is what the
system REFUSES to do.
"""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlparse

import pytest

from hands.artifact import (
    Capability,
    ClickAction,
    NavigateAction,
    RoleRung,
    Step,
    TargetLadder,
    TextVisible,
    UrlMatches,
    risk_review_valid,
    sign_risk_review,
)
from hands.replay import EngineConfig, ReplayEngine
from hands.results import Failure, PolicyViolation, Success
from hands.surface import PlaywrightTimeoutError, WebSurface


def engine(tmp_path: Path) -> ReplayEngine:
    return ReplayEngine(EngineConfig(runs_dir=tmp_path / "runs"))


def impatient_engine(tmp_path: Path) -> ReplayEngine:
    """Same engine, short step budget — for tests whose POINT is the timeout."""
    return ReplayEngine(EngineConfig(runs_dir=tmp_path / "runs", step_budget_s=2.0))


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


def test_failure_after_a_risky_action_warns_the_mutation_may_have_landed(
    capability: Capability, tmp_path: Path
) -> None:
    """A timed-out confirmation page does NOT mean the transfer did not post.
    Observed live five times: the click landed on the POST endpoint and the
    caller was told `failure`. Read as "nothing happened", that invites a
    retry that double-fires an irreversible action — so the report has to say
    the action may already be in flight."""
    capability.steps[1].risk = "risky"
    capability.steps[1].post = [TextVisible(text="a heading this app never renders")]
    signed = sign_risk_review(capability, "reviewer-1")
    result = impatient_engine(tmp_path).run(signed, {"member_id": "12345"})
    assert isinstance(result, Failure)
    assert result.report.mutation_may_have_landed is True


def test_risky_click_that_times_out_mid_dispatch_still_warns(
    capability: Capability, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE named scenario, from the engine's own docstring: 'a click that timed
    out may still have landed server-side'. The click is dispatched and then
    times out. If the flag were set after `_perform` RETURNED, it would read
    False here — in precisely the case it exists for — and the caller would be
    told nothing happened."""
    capability.steps[1].risk = "risky"

    def timeout_after_dispatch(self: WebSurface, resolved: object) -> None:
        raise PlaywrightTimeoutError("click dispatched, confirmation never arrived")

    monkeypatch.setattr(WebSurface, "click", timeout_after_dispatch)
    signed = sign_risk_review(capability, "reviewer-1")
    result = impatient_engine(tmp_path).run(signed, {"member_id": "12345"})
    assert isinstance(result, Failure)
    assert result.report.mutation_may_have_landed is True


def test_a_target_that_never_existed_is_not_read_as_an_action_that_landed(
    capability: Capability, tmp_path: Path
) -> None:
    """The effect probe answers "did my action already land?" — a question that
    only means anything after a DISPATCH. A step whose target does not exist
    dispatched nothing, so if it could reach the probe, a postcondition that
    happens to already hold would be read as proof the step worked. Total UI
    drift would then return SUCCESS with a fabricated value: wrong-but-
    confident, straight through invariant #2."""
    capability.steps.insert(
        1,
        Step(
            id="s_ghost",
            intent="click a control that exists on no page",
            action=ClickAction(),
            target=TargetLadder(
                ladder=[RoleRung(role="button", name="No Such Button Anywhere")]
            ),
            pre=[],
            post=[UrlMatches(pattern="")],  # a postcondition that is ALWAYS true
            risk="safe",
        ),
    )
    result = impatient_engine(tmp_path).run(capability, {"member_id": "12345"})
    assert isinstance(result, Failure), "a step that never resolved must not report success"
    assert result.report.step_id == "s_ghost"


def test_policy_refusal_does_not_inherit_the_previous_runs_evidence(
    capability: Capability, tmp_path: Path
) -> None:
    """The risk gate returns BEFORE a run directory is created. Leaving the
    prior run's `last_run_dir` in place made the API envelope attribute a
    refusal to an unrelated run — and since that envelope now also carries the
    run's audit-chain tip, stale state is a false attestation an auditor would
    'verify' successfully."""
    eng = engine(tmp_path)
    assert isinstance(eng.run(capability, {"member_id": "12345"}), Success)
    assert eng.last_run_dir is not None

    capability.steps[1].risk = "risky"  # now risky and unsigned -> refused
    refused = eng.run(capability, {"member_id": "12345"})
    assert isinstance(refused, PolicyViolation)
    assert eng.last_run_dir is None, "a refused run must not point at someone else's evidence"


def test_allowlist_covers_a_second_page_in_the_same_context(fixture_app: str) -> None:
    """A popup / target=_blank / window.open opens a NEW page in the same
    browser context. A page-scoped route hook carries no handler there at all,
    so its traffic leaves unblocked AND unlogged — no POLICY_VIOLATION, no
    trace line, the run just succeeds."""
    surface = WebSurface()
    try:
        surface.start(fixture_app + "/", allowed_hosts=[urlparse(fixture_app).netloc])
        # A genuine popup, opened by the page itself — not a programmatic
        # new_page(), which Playwright refuses on an owned context anyway.
        surface.page.evaluate("() => window.open('http://127.0.0.1:9/exfiltrate', '_blank')")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if any("127.0.0.1:9" in url for url in surface.blocked_requests):
                break
            time.sleep(0.05)
        assert any("127.0.0.1:9" in url for url in surface.blocked_requests), (
            "the popup's traffic escaped the allowlist unblocked and unlogged"
        )
    finally:
        surface.stop()


def test_failure_before_any_risky_action_claims_no_mutation(
    capability: Capability, tmp_path: Path
) -> None:
    """The flag must stay honest in the other direction: a run that failed
    before touching anything irreversible is safe to retry, and saying
    otherwise would make the warning worthless."""
    capability.steps[0].post = [TextVisible(text="a heading this app never renders")]
    result = impatient_engine(tmp_path).run(capability, {"member_id": "12345"})
    assert isinstance(result, Failure)
    assert result.report.mutation_may_have_landed is False


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
