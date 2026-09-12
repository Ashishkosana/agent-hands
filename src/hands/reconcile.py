"""The reconciler: a pure, deterministic judgment over three inputs.

Given the expected effect, the pre-image and post-image observations from
the independent verifier, and the executor's claim, decide exactly one of:

    VERIFIED_COMMITTED      durable state moved from -> to; effect attributable
    VERIFIED_NOT_COMMITTED  durable state unchanged; nothing happened
    EFFECT_MISMATCH         the executor's DEFINITE claim contradicts durable state
    UNVERIFIABLE            the evidence does not support any of the above

Design rules (the point of the module):

- No I/O, no clock, no browser, no retries. Same inputs -> same verdict.
- UNRESOLVED is not a claim, so it can never MISMATCH; it resolves to
  COMMITTED or NOT_COMMITTED on the evidence, or stays UNVERIFIABLE.
- Insufficient evidence is never rounded up to a verdict. A missing image,
  an identity mismatch, a pre-image already in the target state, an unknown
  status, or a target share that does not appear are all UNVERIFIABLE with
  the reason spelled out.
- ``retry_eligible`` is advice, not an action. Nothing here (or anywhere in
  V1) executes a retry; the flag says only that a retry would not double-
  fire on the evidence available.
- Collateral changes (other shares changing state between images) are
  reported but do not alter the verdict in V1 — forbidden-effect semantics
  are deliberately out of scope until there is evidence of what they should be.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum

from pydantic import BaseModel

from hands.results import (
    BusinessOutcome,
    Failure,
    PolicyViolation,
    PreconditionFailed,
    ReplayResult,
    Unresolved,
)
from hands.verifier import Observation


class Verdict(StrEnum):
    VERIFIED_COMMITTED = "VERIFIED_COMMITTED"
    VERIFIED_NOT_COMMITTED = "VERIFIED_NOT_COMMITTED"
    EFFECT_MISMATCH = "EFFECT_MISMATCH"
    UNVERIFIABLE = "UNVERIFIABLE"


class ExpectedEffect(BaseModel):
    """What the consequential action is supposed to do to durable state."""

    member_number: str
    share_id: str
    from_status: str = "OPEN"
    to_status: str = "HOLD"


class ExecutorClaim(BaseModel):
    """The executor's result, reduced to what the reconciler may use: the
    result kind and, for unresolved runs, whether a mutating request was
    observed leaving the browser. ``committed`` is the executor's claim:
    True (it says it did), False (it says it did not), None (it refuses to
    say — UNRESOLVED)."""

    kind: str
    dispatched: bool | None = None
    detail: str | None = None

    @property
    def committed(self) -> bool | None:
        if self.kind == "success":
            return True
        if self.kind == "unresolved":
            return None
        # failure / precondition_failed / policy_violation: the flow did not
        # reach or complete the action. business_outcome: the server answered
        # with a refusal (e.g. SUPERVISOR_REQUIRED), which is a definite "no".
        return False

    @classmethod
    def from_result(cls, result: ReplayResult) -> ExecutorClaim:
        dispatched: bool | None = None
        detail: str | None = None
        if isinstance(result, Unresolved):
            dispatched = result.dispatched
            detail = result.report.observed
        elif isinstance(result, BusinessOutcome):
            detail = result.code
        elif isinstance(result, Failure):
            detail = result.report.observed
        elif isinstance(result, PreconditionFailed):
            detail = result.unmet
        elif isinstance(result, PolicyViolation):
            detail = result.rule
        return cls(kind=result.result, dispatched=dispatched, detail=detail)


class Reconciliation(BaseModel):
    verdict: Verdict
    reason: str
    retry_eligible: bool
    executor_claim: bool | None
    executor_kind: str
    target_pre: str | None
    target_post: str | None
    collateral_changes: list[str]
    inputs_sha256: str  # hash of the exact inputs judged; re-running is auditable


def _identity_ok(expected: ExpectedEffect, image: Observation) -> bool:
    return image.member_number.strip() == expected.member_number.strip()


def _collateral(pre: Observation, post: Observation, target: str) -> list[str]:
    before = {s.share_id: s.status for s in pre.shares if s.share_id != target}
    after = {s.share_id: s.status for s in post.shares if s.share_id != target}
    changes: list[str] = []
    for share_id in sorted(set(before) | set(after)):
        b, a = before.get(share_id), after.get(share_id)
        if b != a:
            changes.append(f"{share_id}: {b} -> {a}")
    return changes


def reconcile(
    expected: ExpectedEffect,
    pre: Observation | None,
    post: Observation | None,
    executor: ExecutorClaim,
) -> Reconciliation:
    inputs = {
        "expected": expected.model_dump(mode="json"),
        "pre": None if pre is None else pre.model_dump(mode="json"),
        "post": None if post is None else post.model_dump(mode="json"),
        "executor": executor.model_dump(mode="json"),
    }
    digest = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    claim = executor.committed

    def verdict(v: Verdict, reason: str, *, retry: bool = False,
                t_pre: str | None = None, t_post: str | None = None,
                collateral: list[str] | None = None) -> Reconciliation:
        return Reconciliation(
            verdict=v, reason=reason, retry_eligible=retry, executor_claim=claim,
            executor_kind=executor.kind, target_pre=t_pre, target_post=t_post,
            collateral_changes=collateral or [], inputs_sha256=digest,
        )

    U = Verdict.UNVERIFIABLE
    if pre is None and post is None:
        return verdict(U, "neither pre-image nor post-image is available")
    if pre is None:
        return verdict(U, "pre-image unavailable: the effect cannot be attributed to this run")
    if post is None:
        return verdict(U, "post-image unavailable: durable state could not be read",
                       t_pre=pre.status_of(expected.share_id))
    if not _identity_ok(expected, pre):
        return verdict(U, f"pre-image identity mismatch: page shows member "
                          f"{pre.member_number!r}, expected {expected.member_number!r}")
    if not _identity_ok(expected, post):
        return verdict(U, f"post-image identity mismatch: page shows member "
                          f"{post.member_number!r}, expected {expected.member_number!r}")

    t_pre = pre.status_of(expected.share_id)
    t_post = post.status_of(expected.share_id)
    collateral = _collateral(pre, post, expected.share_id)
    if t_pre is None:
        return verdict(U, f"target share {expected.share_id!r} absent from the pre-image",
                       t_post=t_post, collateral=collateral)
    if t_post is None:
        return verdict(U, f"target share {expected.share_id!r} absent from the post-image",
                       t_pre=t_pre, collateral=collateral)
    if t_pre != expected.from_status:
        return verdict(
            U,
            f"pre-image status {t_pre!r} is not the expected starting state "
            f"{expected.from_status!r}; a post-image of {t_post!r} proves nothing about this run",
            t_pre=t_pre, t_post=t_post, collateral=collateral,
        )

    if t_post == expected.to_status:
        if claim is False:
            return verdict(
                Verdict.EFFECT_MISMATCH,
                f"executor reported NOT committed ({executor.kind}"
                f"{': ' + executor.detail if executor.detail else ''}) but durable state moved "
                f"{t_pre} -> {t_post}",
                t_pre=t_pre, t_post=t_post, collateral=collateral,
            )
        how = "executor confirmed" if claim else "executor was unresolved"
        return verdict(
            Verdict.VERIFIED_COMMITTED,
            f"durable state moved {t_pre} -> {t_post}; {how}",
            t_pre=t_pre, t_post=t_post, collateral=collateral,
        )
    if t_post == expected.from_status:
        if claim is True:
            return verdict(
                Verdict.EFFECT_MISMATCH,
                f"executor reported success but durable state is unchanged ({t_post})",
                t_pre=t_pre, t_post=t_post, collateral=collateral,
            )
        how = "executor was unresolved" if claim is None else f"executor reported {executor.kind}"
        # A retry cannot double-fire if nothing landed; whether it SHOULD run
        # is policy (risk review, operator), decided elsewhere. A server
        # refusal (business outcome) is not retry-eligible: same input, same no.
        retry = executor.kind != "business_outcome"
        return verdict(
            Verdict.VERIFIED_NOT_COMMITTED,
            f"durable state unchanged ({t_post}); {how}",
            retry=retry, t_pre=t_pre, t_post=t_post, collateral=collateral,
        )
    return verdict(
        U,
        f"post-image status {t_post!r} is neither {expected.from_status!r} nor "
        f"{expected.to_status!r}; no rule covers it",
        t_pre=t_pre, t_post=t_post, collateral=collateral,
    )
