"""The reconciler is a pure function; these tests pin every branch.

The property that matters most: insufficient evidence is never converted
into a confident verdict. Every "missing / mismatched / unexpected" input
maps to UNVERIFIABLE with a reason, and an UNRESOLVED executor can never
produce EFFECT_MISMATCH.
"""

from __future__ import annotations

import itertools

import pytest

from hands.reconcile import ExecutorClaim, ExpectedEffect, Verdict, reconcile
from hands.results import (
    BusinessOutcome,
    Failure,
    FailureReport,
    MatchEvidence,
    PolicyViolation,
    PreconditionFailed,
    Success,
    Unresolved,
)
from hands.verifier import Observation, ShareStatus

EXPECTED = ExpectedEffect(member_number="100234", share_id="100234-S0070")


def image(target: str | None = "OPEN", member: str = "100234", other: str = "OPEN",
          extra: dict[str, str] | None = None) -> Observation:
    shares = [ShareStatus(share_id="100234-S0001", type="Regular Shares", status=other,
                          status_raw=other)]
    if target is not None:
        shares.append(ShareStatus(share_id="100234-S0070", type="Share Draft (Checking)",
                                  status=target, status_raw=f"{target} [x]"))
    for sid, st in (extra or {}).items():
        shares.append(ShareStatus(share_id=sid, type="Other", status=st, status_raw=st))
    return Observation(member_number=member, shares=shares, observed_at="t", page_url="u")


SUCCESS = ExecutorClaim(kind="success")
UNRESOLVED = ExecutorClaim(kind="unresolved", dispatched=True)
FAILURE = ExecutorClaim(kind="failure", detail="x")
REFUSED = ExecutorClaim(kind="business_outcome", detail="SUPERVISOR_REQUIRED")
ALL_CLAIMS = [SUCCESS, UNRESOLVED, FAILURE, REFUSED,
              ExecutorClaim(kind="precondition_failed"), ExecutorClaim(kind="policy_violation")]


# ----------------------------------------------------------------- the matrix


def test_committed_with_confirming_executor() -> None:
    rec = reconcile(EXPECTED, image("OPEN"), image("HOLD"), SUCCESS)
    assert rec.verdict is Verdict.VERIFIED_COMMITTED
    assert rec.retry_eligible is False
    assert (rec.target_pre, rec.target_post) == ("OPEN", "HOLD")


def test_committed_with_unresolved_executor_is_the_lost_ack_case() -> None:
    rec = reconcile(EXPECTED, image("OPEN"), image("HOLD"), UNRESOLVED)
    assert rec.verdict is Verdict.VERIFIED_COMMITTED
    assert rec.executor_claim is None
    assert "unresolved" in rec.reason


def test_not_committed_with_unresolved_executor_is_retry_eligible() -> None:
    rec = reconcile(EXPECTED, image("OPEN"), image("OPEN"), UNRESOLVED)
    assert rec.verdict is Verdict.VERIFIED_NOT_COMMITTED
    assert rec.retry_eligible is True


def test_not_committed_with_failure_is_consistent_and_retry_eligible() -> None:
    rec = reconcile(EXPECTED, image("OPEN"), image("OPEN"), FAILURE)
    assert rec.verdict is Verdict.VERIFIED_NOT_COMMITTED
    assert rec.retry_eligible is True


def test_server_refusal_is_not_committed_and_not_retry_eligible() -> None:
    rec = reconcile(EXPECTED, image("OPEN"), image("OPEN"), REFUSED)
    assert rec.verdict is Verdict.VERIFIED_NOT_COMMITTED
    assert rec.retry_eligible is False  # same input would get the same no


def test_false_success_is_a_mismatch() -> None:
    rec = reconcile(EXPECTED, image("OPEN"), image("OPEN"), SUCCESS)
    assert rec.verdict is Verdict.EFFECT_MISMATCH
    assert "success" in rec.reason and "unchanged" in rec.reason


def test_claimed_failure_but_state_moved_is_a_mismatch_too() -> None:
    rec = reconcile(EXPECTED, image("OPEN"), image("HOLD"), FAILURE)
    assert rec.verdict is Verdict.EFFECT_MISMATCH
    assert "NOT committed" in rec.reason


def test_unresolved_can_never_mismatch() -> None:
    for pre, post in itertools.product(["OPEN", "HOLD", "CLOSED", None], repeat=2):
        rec = reconcile(EXPECTED, image(pre), image(post), UNRESOLVED)
        assert rec.verdict is not Verdict.EFFECT_MISMATCH, (pre, post)


# ------------------------------------------------------- insufficient evidence


@pytest.mark.parametrize("claim", ALL_CLAIMS, ids=lambda c: c.kind)
def test_missing_images_are_unverifiable_regardless_of_claim(claim: ExecutorClaim) -> None:
    assert reconcile(EXPECTED, None, None, claim).verdict is Verdict.UNVERIFIABLE
    assert reconcile(EXPECTED, None, image("HOLD"), claim).verdict is Verdict.UNVERIFIABLE
    assert reconcile(EXPECTED, image("OPEN"), None, claim).verdict is Verdict.UNVERIFIABLE


def test_post_image_missing_names_the_reason_and_keeps_pre_status() -> None:
    rec = reconcile(EXPECTED, image("OPEN"), None, SUCCESS)
    assert "post-image unavailable" in rec.reason
    assert rec.target_pre == "OPEN" and rec.target_post is None


def test_identity_mismatch_is_unverifiable_even_when_statuses_look_right() -> None:
    rec = reconcile(EXPECTED, image("OPEN"), image("HOLD", member="100987"), SUCCESS)
    assert rec.verdict is Verdict.UNVERIFIABLE
    assert "identity mismatch" in rec.reason
    rec = reconcile(EXPECTED, image("OPEN", member="100987"), image("HOLD"), SUCCESS)
    assert rec.verdict is Verdict.UNVERIFIABLE


def test_pre_image_already_in_target_state_proves_nothing() -> None:
    rec = reconcile(EXPECTED, image("HOLD"), image("HOLD"), SUCCESS)
    assert rec.verdict is Verdict.UNVERIFIABLE
    assert "not the expected starting state" in rec.reason


def test_target_share_absent_is_unverifiable() -> None:
    assert reconcile(EXPECTED, image(None), image("HOLD"), SUCCESS).verdict is Verdict.UNVERIFIABLE
    assert reconcile(EXPECTED, image("OPEN"), image(None), SUCCESS).verdict is Verdict.UNVERIFIABLE


def test_unknown_post_status_has_no_rule() -> None:
    rec = reconcile(EXPECTED, image("OPEN"), image("CLOSED"), SUCCESS)
    assert rec.verdict is Verdict.UNVERIFIABLE
    assert "no rule covers it" in rec.reason


# --------------------------------------------------------- collateral & audit


def test_collateral_changes_are_reported_but_do_not_change_the_verdict() -> None:
    rec = reconcile(EXPECTED, image("OPEN", other="OPEN"), image("HOLD", other="HOLD"), SUCCESS)
    assert rec.verdict is Verdict.VERIFIED_COMMITTED
    assert rec.collateral_changes == ["100234-S0001: OPEN -> HOLD"]


def test_reconciliation_is_deterministic_and_hashes_its_inputs() -> None:
    a = reconcile(EXPECTED, image("OPEN"), image("HOLD"), UNRESOLVED)
    b = reconcile(EXPECTED, image("OPEN"), image("HOLD"), UNRESOLVED)
    assert a == b
    c = reconcile(EXPECTED, image("OPEN"), image("HOLD"), SUCCESS)
    assert c.inputs_sha256 != a.inputs_sha256


def test_executor_claim_from_every_result_kind() -> None:
    report = FailureReport(step_id="s13", intent="apply", expected="e", observed="o")
    assert ExecutorClaim.from_result(Success(outputs={})).committed is True
    unresolved = ExecutorClaim.from_result(
        Unresolved(step_id="s13", intent="apply", dispatched=True, report=report)
    )
    assert unresolved.committed is None and unresolved.dispatched is True
    assert ExecutorClaim.from_result(Failure(report=report)).committed is False
    outcome = BusinessOutcome(
        code="SUPERVISOR_REQUIRED", description="d",
        evidence=MatchEvidence(condition_id="c", matched_text="m"),
    )
    assert ExecutorClaim.from_result(outcome).detail == "SUPERVISOR_REQUIRED"
    assert ExecutorClaim.from_result(PreconditionFailed(unmet="u", observed="o")).committed is False
    assert ExecutorClaim.from_result(PolicyViolation(rule="r", detail="d")).committed is False
