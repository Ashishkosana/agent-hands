"""Regression tests for the independent review of V1 (PR #11).

F1  uncertainty is preserved after ANY consequential dispatch — a corrupted
    acknowledgement, a failed output read, a failed later step, a recovery
    that would re-run the risky step — and the twin still reconciles from
    durable state.
F2  a verdict is a statement about the observation window, not causation:
    a third party's mutation inside the window yields VERIFIED_COMMITTED with
    attribution="window", the window recorded, and no causal wording.
F3  UNRESOLVED requires a consequential dispatch: bad credentials (a sign-on
    POST) are a FAILURE; a definite server refusal is a BUSINESS_OUTCOME.
F7  the read-only guard matches the sign-on path exactly.
F8  the chaos record's upstream HTTP status is not evidence of commit.
"""

from __future__ import annotations

import http.cookiejar
import json
import re
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from hands.artifact import (
    Capability,
    ClickAction,
    RoleNameVisible,
    Step,
    TargetLadder,
    TextRung,
    dump_capability,
    load_capability,
    sign_risk_review,
)
from hands.chaos import ChaosMode, FaultInjector
from hands.reconcile import ExpectedEffect, Verdict
from hands.replay import EngineConfig, ReplayEngine
from hands.results import BusinessOutcome, Failure, Unresolved
from hands.twin import TwinConfig, TwinRun, retarget, run_twin, verdict_of
from hands.verifier import ReadOnlyGuard
from tests.conftest import REPO_ROOT, meridian_reset, meridian_truth

GEN = REPO_ROOT / "capabilities" / "generated"
MEMBER, SHARE = "100234", "100234-S0070"
EXECUTOR_PARAMS = {
    "operator_id": "super1", "password": "password", "member_number": MEMBER,
    "share": "Share Draft (Checking)", "reason": "FRAUD", "notes": "review regression",
}
VERIFIER_PARAMS = {"operator_id": "teller1", "password": "password", "member_number": MEMBER}
BUDGET = 4.0


@pytest.fixture(scope="module")
def artifacts(meridian_app: str, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    work = tmp_path_factory.mktemp("review-artifacts")
    paths: dict[str, Path] = {}
    for name in ("meridian_place_hold", "meridian_member_record"):
        cap = retarget(load_capability(GEN / f"{name}.json"), meridian_app, "test-harness")
        paths[name] = work / f"{name}.json"
        paths[name].write_text(dump_capability(cap))
    return paths


@pytest.fixture()
def fresh(meridian_app: str) -> Iterator[str]:
    meridian_reset(meridian_app)
    yield meridian_app
    meridian_reset(meridian_app)


def truth(base: str, share: str = SHARE) -> str:
    shares = meridian_truth(base)["shares"]
    assert isinstance(shares, dict)
    return str(shares[share])


def twin(artifacts: dict[str, Path], tmp_path: Path, injector: FaultInjector | None,
         executor_artifact: Path | None = None) -> TwinRun:
    return run_twin(TwinConfig(
        executor_artifact=executor_artifact or artifacts["meridian_place_hold"],
        verifier_artifact=artifacts["meridian_member_record"],
        executor_params=EXECUTOR_PARAMS,
        verifier_params=VERIFIER_PARAMS,
        expected=ExpectedEffect(member_number=MEMBER, share_id=SHARE),
        runs_dir=tmp_path / "runs",
        executor_chaos=injector,
        step_budget_s=BUDGET,
    ))


def executor_events(run: TwinRun) -> list[dict[str, object]]:
    run_dir = run.manifest.run_dirs["executor"]
    assert run_dir is not None
    return [json.loads(ln) for ln in (Path(run_dir) / "trace.jsonl").read_text().splitlines()]


# ------------------------------------------------------------------------ F1


def test_f1_corrupted_acknowledgement_after_a_real_commit_is_unresolved_not_failure(
    fresh: str, artifacts: dict[str, Path], tmp_path: Path
) -> None:
    """The reviewer's probe: the hold commits, the acknowledgement page comes
    back, but the identity the checkpoint binds to is unreadable. Before the
    fix the engine returned FAILURE with the durable state already mutated."""
    injector = FaultInjector(ChaosMode.COMMIT_WITH_CORRUPT_ACK)
    run = twin(artifacts, tmp_path, injector)
    assert injector.fired and injector.fired[0]["corrupted"] == "identity"
    assert injector.fired[0]["ack_altered"] is True
    assert truth(fresh) == "HOLD"  # it really committed
    assert run.executor is not None and run.executor.result_kind == "unresolved"
    result = run.executor.result
    assert result["step_id"] == "s13"  # named for the consequential action in doubt...
    report = result["report"]
    assert isinstance(report, dict) and report["intent"] == "verify checkpoint"  # ...not the phase
    assert result["dispatched"] is True
    kinds = [e["event"] for e in executor_events(run)]
    assert "consequential_dispatch" in kinds and "postconditions_met" in kinds
    assert "unresolved" in kinds and "failure" not in kinds
    # The twin settles it from durable state.
    assert verdict_of(run) == Verdict.VERIFIED_COMMITTED


def test_f1_failed_output_extraction_after_a_real_commit_is_unresolved(
    fresh: str, artifacts: dict[str, Path], tmp_path: Path
) -> None:
    """Checkpoint verifies (heading + identity) but the confirmation-number
    cell is gone. Output extraction fails AFTER the commit: still UNRESOLVED."""
    injector = FaultInjector(ChaosMode.COMMIT_WITH_CORRUPT_OUTPUT)
    run = twin(artifacts, tmp_path, injector)
    assert injector.fired and injector.fired[0]["corrupted"] == "output"
    assert truth(fresh) == "HOLD"
    assert run.executor is not None and run.executor.result_kind == "unresolved"
    report = run.executor.result["report"]
    assert isinstance(report, dict) and str(report["intent"]).startswith("extract output")
    kinds = [e["event"] for e in executor_events(run)]
    assert "checkpoint_verified" in kinds  # the failure came after the checkpoint
    assert verdict_of(run) == Verdict.VERIFIED_COMMITTED


def _with_impossible_final_step(cap: Capability) -> Capability:
    """Append a safe step after the risky one whose target cannot exist, so
    the run fails at a LATER step than the consequential action."""
    doc = cap.model_dump(mode="json", by_alias=True)
    doc["risk_review"] = {"reviewed_by": None, "artifact_hash": None}
    extended = Capability.model_validate(doc)
    extended.steps.append(Step(
        id="s14", intent="Open a screen that does not exist", risk="safe",
        target=TargetLadder(ladder=[TextRung(text="Escalate To Compliance Desk")]),
        action=ClickAction(kind="click"), pre=[],
        post=[RoleNameVisible(role="heading", name="COMPLIANCE DESK")],
    ))
    return sign_risk_review(extended, "test-harness")


def test_f1_failure_of_a_later_step_after_a_real_commit_is_unresolved(
    fresh: str, artifacts: dict[str, Path], tmp_path: Path
) -> None:
    cap = _with_impossible_final_step(load_capability(artifacts["meridian_place_hold"]))
    path = tmp_path / "hold_plus_step.json"
    path.write_text(dump_capability(cap))
    run = twin(artifacts, tmp_path, None, executor_artifact=path)
    assert truth(fresh) == "HOLD"
    assert run.executor is not None and run.executor.result_kind == "unresolved"
    result = run.executor.result
    assert result["step_id"] == "s13"
    report = result["report"]
    assert isinstance(report, dict) and report["step_id"] == "s14"
    assert verdict_of(run) == Verdict.VERIFIED_COMMITTED


def test_f1_recovery_may_not_resume_at_or_before_the_consequential_step(
    tmp_path: Path,
) -> None:
    """White-box: once a hazard is recorded, a recoverable condition whose
    restart point precedes the consequential step is refused (it would
    re-run the mutation) and the refusal preserves uncertainty."""
    from hands.artifact import Condition, NavigateAction, RoleNameMatch
    from hands.conditions import RecognizerHit
    from hands.replay import _Hazard, _Recover, _StepFailed
    from hands.results import MatchEvidence

    cap = load_capability(GEN / "meridian_place_hold.json")
    cond = Condition(
        id="cond_heal", armed_after="s4",
        match=RoleNameMatch(role="heading", name="OPERATOR SIGN ON"),
        classify="recoverable", recovery=[Step(
            id="r1", intent="Go back to sign-on", risk="safe",
            action=NavigateAction(kind="navigate", url=cap.target.entry.web.url),
            post=[RoleNameVisible(role="heading", name="OPERATOR SIGN ON")],
        )],
        resume="restart_from", restart_from="s4", max_fires_per_run=2, provenance="authored",
    )
    engine = ReplayEngine(EngineConfig(runs_dir=tmp_path))
    engine._hazard = _Hazard(step_id="s13", intent="Apply the hold",
                             requests=("POST http://t/members/100234/hold/post",))
    step_order = {s.id: i for i, s in enumerate(cap.steps)}

    class _Trace:
        def emit(self, event: str, **fields: object) -> None:
            pass

    class _Surface:
        def __init__(self) -> None:
            self.dispatched: list[object] = []

    hit = RecognizerHit(condition=cond,
                        evidence=MatchEvidence(condition_id=cond.id,
                                               matched_text="OPERATOR SIGN ON"))
    with pytest.raises(_StepFailed) as failed:
        engine._recover(cap, {}, _Surface(), _Trace(), _Recover(hit, acted=False),  # type: ignore[arg-type]
                        fires={}, step_order=step_order, current=13, step=None)
    assert "re-running a consequential action" in failed.value.report.observed


# ------------------------------------------------------------------------ F2


def third_party_hold(base_url: str) -> FaultInjector:
    """A different actor: its own cookie jar, its own sign-on, the real UI
    flow (form -> review -> post). Nothing shared with the executor."""

    def act(member: str, share_id: str) -> None:
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

        def post(path: str, data: dict[str, str]) -> str:
            body = urllib.parse.urlencode(data).encode()
            with opener.open(base_url + path, data=body, timeout=5) as r:
                return str(r.read().decode())

        post("/signon", {"operator": "super1", "password": "password", "branch": "001"})
        with opener.open(f"{base_url}/members/{member}/hold", timeout=5) as r:
            form = r.read().decode()
        token_match = re.search(r'name="_token" value="([^"]+)"', form)
        assert token_match is not None
        token = token_match.group(1)
        fields = {"_token": token, "share": share_id, "reason": "LEGAL", "notes": "3rd party"}
        post(f"/members/{member}/hold/review", fields)
        page = post(f"/members/{member}/hold/post", fields)
        assert "ACCOUNT HOLD APPLIED" in page

    return FaultInjector(ChaosMode.THIRD_PARTY_MUTATION, third_party=act)


def test_f2_third_party_mutation_in_the_window_is_reported_as_window_attribution_only(
    fresh: str, artifacts: dict[str, Path], tmp_path: Path
) -> None:
    """The executor's POST is dropped; someone else places the hold inside
    the window. Durable state moved OPEN -> HOLD, so the verdict is
    VERIFIED_COMMITTED — and the verdict must say, in its own fields and
    words, that this is window attribution, not proof this run did it."""
    injector = third_party_hold(fresh)
    run = twin(artifacts, tmp_path, injector)
    assert injector.fired and injector.fired[0]["forwarded"] is False
    assert injector.fired[0]["third_party_mutated"] == SHARE
    holds = meridian_truth(fresh)["holds"]
    assert isinstance(holds, list) and len(holds) == 1
    assert holds[0]["reason"] == "LEGAL"  # the third party's hold, not this run's FRAUD hold
    assert truth(fresh) == "HOLD"
    assert run.executor is not None and run.executor.result_kind == "unresolved"
    rec = run.reconciliation
    assert rec is not None and rec.verdict == Verdict.VERIFIED_COMMITTED
    assert rec.attribution == "window"
    assert rec.pre_observed_at is not None and rec.post_observed_at is not None
    assert rec.observation_window_ms is not None and rec.observation_window_ms > 0
    assert "does not establish that this run caused" in rec.reason
    forbidden = ("attributable", "caused by this run", "confirms this run")
    assert not any(word in rec.reason.lower() for word in forbidden)


# ------------------------------------------------------------------------ F3


def test_f3_bad_credentials_are_a_failure_not_unresolved(
    fresh: str, artifacts: dict[str, Path], tmp_path: Path
) -> None:
    """The sign-on click is labeled risky in every planner-derived artifact
    (it is a POST). Its POST is session establishment, not a business
    mutation: a rejected login is a FAILURE."""
    cap = load_capability(artifacts["meridian_place_hold"])
    engine = ReplayEngine(EngineConfig(runs_dir=tmp_path / "runs", step_budget_s=BUDGET))
    result = engine.run(cap, {**EXECUTOR_PARAMS, "password": "not-the-password"})
    assert isinstance(result, Failure), result
    assert result.report.step_id == "s3"
    assert truth(fresh) == "OPEN"
    events = [json.loads(ln) for ln in
              (Path(engine.last_run_dir or "") / "trace.jsonl").read_text().splitlines()]
    assert "consequential_dispatch" not in [e["event"] for e in events]


def test_f3_definite_server_refusal_after_dispatch_is_a_business_outcome(
    fresh: str, artifacts: dict[str, Path], tmp_path: Path
) -> None:
    """Share 103001-S0001 is seeded HOLD. The fixture refuses the re-hold on
    the real /hold/post with the console's TRANSACTION REJECTED wording and
    HTTP 200. A consequential request WAS dispatched, and the server gave a
    definite answer: BUSINESS_OUTCOME, never UNRESOLVED."""
    cap = load_capability(artifacts["meridian_place_hold"])
    engine = ReplayEngine(EngineConfig(runs_dir=tmp_path / "runs", step_budget_s=BUDGET))
    params = {**EXECUTOR_PARAMS, "member_number": "103001", "share": "Regular Shares"}
    result = engine.run(cap, params)
    assert isinstance(result, BusinessOutcome), result
    assert result.code == "TRANSACTION_REJECTED"
    assert "could not be completed as entered" in result.evidence.matched_text
    assert meridian_truth(fresh)["holds"] == []
    events = [json.loads(ln) for ln in
              (Path(engine.last_run_dir or "") / "trace.jsonl").read_text().splitlines()]
    kinds = [e["event"] for e in events]
    assert "consequential_dispatch" in kinds  # the POST did leave the browser...
    assert "unresolved" not in kinds  # ...and the definite refusal settled it


def test_f3_unresolved_is_never_produced_without_a_consequential_dispatch(
    fresh: str, artifacts: dict[str, Path], tmp_path: Path
) -> None:
    """Regression guard for the rule itself, using the executor across the
    scenarios in this module: every UNRESOLVED carries dispatched=True."""
    cap = load_capability(artifacts["meridian_place_hold"])
    engine = ReplayEngine(EngineConfig(
        runs_dir=tmp_path / "runs", step_budget_s=BUDGET,
        interceptor=FaultInjector(ChaosMode.NO_COMMIT_WITH_LOST_ACK),
    ))
    result = engine.run(cap, EXECUTOR_PARAMS)
    assert isinstance(result, Unresolved) and result.dispatched is True


# ------------------------------------------------------------------------ F7


class _Req:
    def __init__(self, method: str, url: str) -> None:
        self.method = method
        self.url = url


class _Route:
    def __init__(self) -> None:
        self.aborted: str | None = None

    def abort(self, error_code: str = "failed") -> None:
        self.aborted = error_code


@pytest.mark.parametrize(
    ("method", "url", "blocked"),
    [
        ("GET", "https://h/anything?x=1", False),
        ("POST", "https://h/signon", False),
        ("POST", "https://h/signon?next=/menu", False),
        ("POST", "https://h/members/1/signon", True),
        ("POST", "https://h/signon/extra", True),
        ("POST", "https://h/signonx", True),
        ("PUT", "https://h/signon", True),
        ("POST", "https://h/members/100234/hold/post", True),
    ],
)
def test_f7_read_only_guard_matches_the_sign_on_path_exactly(
    method: str, url: str, blocked: bool
) -> None:
    guard = ReadOnlyGuard()
    route = _Route()
    handled = guard(route, _Req(method, url))  # type: ignore[arg-type]
    assert handled is blocked
    assert (route.aborted == "accessdenied") is blocked
    assert bool(guard.violations) is blocked


# ------------------------------------------------------------------------ F8


def test_f8_upstream_status_is_recorded_as_transport_not_commit_evidence(
    fresh: str, artifacts: dict[str, Path], tmp_path: Path
) -> None:
    """The fixture answers a definite refusal with HTTP 200 on the real
    /hold/post. A lost ack on that request records upstream 200 — and the
    twin still says NOT committed, because durable state did not move."""
    cap = load_capability(artifacts["meridian_place_hold"])
    injector = FaultInjector(ChaosMode.COMMIT_WITH_LOST_ACK)
    run = run_twin(TwinConfig(
        executor_artifact=artifacts["meridian_place_hold"],
        verifier_artifact=artifacts["meridian_member_record"],
        executor_params={**EXECUTOR_PARAMS, "member_number": "103001", "share": "Regular Shares"},
        verifier_params={**VERIFIER_PARAMS, "member_number": "103001"},
        expected=ExpectedEffect(member_number="103001", share_id="103001-S0001",
                                from_status="HOLD", to_status="RELEASED"),
        runs_dir=tmp_path / "runs",
        executor_chaos=injector,
        step_budget_s=BUDGET,
    ))
    assert cap.name == "meridian_place_hold"
    assert injector.fired and injector.fired[0]["upstream_http_status"] == 200
    assert "server_status" not in injector.fired[0]
    assert run.executor is not None and run.executor.result_kind == "unresolved"
    rec = run.reconciliation
    assert rec is not None and rec.verdict == Verdict.VERIFIED_NOT_COMMITTED
    assert meridian_truth(fresh)["holds"] == []
