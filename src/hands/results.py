"""The replay result contract.

Every replay ends in exactly one of these shapes. The distinction that matters:
a BusinessOutcome is a legitimate *answer* the caller needs ("no such member"),
not a malfunction. Conflating answers with failures is the classic mistake in
this problem domain, so the contract makes the distinction structural.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field

OutputValue = str | Decimal


class MatchEvidence(BaseModel):
    """Where and what a condition recognizer matched — auditable, not a bare claim."""

    condition_id: str
    region: str | None = None
    role: str | None = None
    matched_text: str


class Success(BaseModel):
    result: Literal["success"] = "success"
    outputs: dict[str, OutputValue]


class BusinessOutcome(BaseModel):
    result: Literal["business_outcome"] = "business_outcome"
    code: str
    description: str
    evidence: MatchEvidence


class FailureReport(BaseModel):
    """Enough detail to debug: which step, what was expected, what was observed."""

    step_id: str | None
    intent: str | None
    expected: str
    observed: str
    screenshot_path: str | None = None
    snapshot_path: str | None = None
    # True when a `risky` step's action was performed before this run failed.
    # A timed-out confirmation page does not mean the transfer did not post, so
    # "it failed" must not be read as "nothing happened": the caller has to
    # verify state before retrying, or it double-fires the irreversible action.
    mutation_may_have_landed: bool = False


class Failure(BaseModel):
    result: Literal["failure"] = "failure"
    report: FailureReport


class PreconditionFailed(BaseModel):
    """The environment is wrong (not authenticated, wrong entry state) — distinct
    from a step failure so the caller isn't misled by a locator error on a login
    page."""

    result: Literal["precondition_failed"] = "precondition_failed"
    unmet: str
    observed: str


class PolicyViolation(BaseModel):
    """The guardrails stopped the run — off-allowlist traffic, or an
    unattended replay of risky steps without a signed risk review. Distinct
    from Failure: the flow didn't break; policy refused it."""

    result: Literal["policy_violation"] = "policy_violation"
    rule: str
    detail: str


ReplayResult = Annotated[
    Success | BusinessOutcome | Failure | PreconditionFailed | PolicyViolation,
    Field(discriminator="result"),
]
