"""End-to-end verified runs against the MERIDIAN-shaped local fixture.

Each test injects one fault at the request seam, runs the full
pre-image -> execute -> post-image -> reconcile sequence with the REAL
artifacts (retargeted to the fixture), and checks the verdict against the
fixture's ground truth (/__state) — the durable state itself, not the UI.

The acceptance criterion of V1 lives here: zero cases where insufficient
evidence became a confident verdict.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from hands.artifact import dump_capability, load_capability
from hands.chaos import ChaosMode, FaultInjector
from hands.reconcile import ExpectedEffect, Verdict
from hands.trace import verify_chain
from hands.twin import TwinConfig, TwinRun, retarget, run_twin, verdict_of
from hands.verifier import observe_in_process
from tests.conftest import REPO_ROOT, meridian_reset, meridian_truth

GEN = REPO_ROOT / "capabilities" / "generated"
MEMBER, SHARE = "100234", "100234-S0070"
EXECUTOR_PARAMS = {
    "operator_id": "super1", "password": "password", "member_number": MEMBER,
    "share": "Share Draft (Checking)", "reason": "FRAUD", "notes": "verifier twin test",
}
VERIFIER_PARAMS = {"operator_id": "teller1", "password": "password", "member_number": MEMBER}


@pytest.fixture(scope="module")
def artifacts(meridian_app: str, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    work = tmp_path_factory.mktemp("twin-artifacts")
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


def twin(artifacts: dict[str, Path], tmp_path: Path, *, executor_chaos: ChaosMode | None = None,
         verifier_chaos: ChaosMode | None = None) -> tuple[TwinRun, FaultInjector | None]:
    injector = FaultInjector(executor_chaos) if executor_chaos else None
    cfg = TwinConfig(
        executor_artifact=artifacts["meridian_place_hold"],
        verifier_artifact=artifacts["meridian_member_record"],
        executor_params=EXECUTOR_PARAMS,
        verifier_params=VERIFIER_PARAMS,
        expected=ExpectedEffect(member_number=MEMBER, share_id=SHARE),
        runs_dir=tmp_path / "runs",
        executor_chaos=injector,
        verifier_chaos=verifier_chaos,
        step_budget_s=4.0,  # the fixture answers in ms; a lost ack is a wait, not a race
    )
    return run_twin(cfg), injector


def truth(base: str) -> str:
    shares = meridian_truth(base)["shares"]
    assert isinstance(shares, dict)
    return str(shares[SHARE])


# ------------------------------------------------------------------ scenarios


def test_baseline_commit_is_verified_by_an_independent_read(
    fresh: str, artifacts: dict[str, Path], tmp_path: Path
) -> None:
    run, _ = twin(artifacts, tmp_path)
    assert run.executor is not None and run.executor.result_kind == "success"
    assert verdict_of(run) == Verdict.VERIFIED_COMMITTED
    assert truth(fresh) == "HOLD"
    assert run.pre is not None and run.pre.operator_id == "teller1"  # not the executor's login


def test_commit_with_lost_ack_executor_cannot_know_verifier_can(
    fresh: str, artifacts: dict[str, Path], tmp_path: Path
) -> None:
    run, injector = twin(artifacts, tmp_path, executor_chaos=ChaosMode.COMMIT_WITH_LOST_ACK)
    assert injector is not None and injector.fired[0]["server_status"] == 200
    assert injector.fired[0]["response_withheld"] is True
    # The executor refused to claim either way — and recorded the one fact it had.
    assert run.executor is not None and run.executor.result_kind == "unresolved"
    assert run.executor.result["dispatched"] is True
    # Durable state moved; the independent read saw it; the reconciler attributed it.
    assert truth(fresh) == "HOLD"
    assert verdict_of(run) == Verdict.VERIFIED_COMMITTED
    assert run.reconciliation is not None and run.reconciliation.executor_claim is None


def test_no_commit_with_lost_ack_is_verified_not_committed_and_nothing_is_retried(
    fresh: str, artifacts: dict[str, Path], tmp_path: Path
) -> None:
    run, _ = twin(artifacts, tmp_path, executor_chaos=ChaosMode.NO_COMMIT_WITH_LOST_ACK)
    assert run.executor is not None and run.executor.result_kind == "unresolved"
    assert truth(fresh) == "OPEN"
    assert verdict_of(run) == Verdict.VERIFIED_NOT_COMMITTED
    assert run.reconciliation is not None and run.reconciliation.retry_eligible is True
    # retry_eligible is advice: the fixture journal shows no hold was ever posted.
    assert meridian_truth(fresh)["holds"] == []


def test_false_success_is_caught_as_effect_mismatch(
    fresh: str, artifacts: dict[str, Path], tmp_path: Path
) -> None:
    run, injector = twin(artifacts, tmp_path, executor_chaos=ChaosMode.FALSE_SUCCESS)
    assert injector is not None and injector.fired[0]["fabricated"] == "ACCOUNT HOLD APPLIED"
    # The executor was fooled: heading, identity-bound checkpoint, and output all matched.
    assert run.executor is not None and run.executor.result_kind == "success"
    assert run.executor.result["outputs"] == {"confirmation_number": "CN000000"}
    assert truth(fresh) == "OPEN"
    assert verdict_of(run) == Verdict.EFFECT_MISMATCH


def test_verifier_failure_fails_closed_even_when_the_effect_really_happened(
    fresh: str, artifacts: dict[str, Path], tmp_path: Path
) -> None:
    run, _ = twin(artifacts, tmp_path, verifier_chaos=ChaosMode.VERIFIER_FAILURE)
    assert run.executor is not None and run.executor.result_kind == "success"
    assert truth(fresh) == "HOLD"  # it DID commit...
    assert run.post is not None and run.post.ok is False
    assert verdict_of(run) == Verdict.UNVERIFIABLE  # ...and the system still refuses to say so
    assert run.reconciliation is not None
    assert "post-image unavailable" in run.reconciliation.reason


def test_pre_image_already_on_hold_refuses_to_execute(
    fresh: str, artifacts: dict[str, Path], tmp_path: Path
) -> None:
    first, _ = twin(artifacts, tmp_path)
    assert verdict_of(first) == Verdict.VERIFIED_COMMITTED
    holds_before = meridian_truth(fresh)["holds"]
    second, _ = twin(artifacts, tmp_path)
    assert second.executed is False and second.aborted is not None
    assert "HOLD" in second.aborted and verdict_of(second) == "REFUSED"
    assert meridian_truth(fresh)["holds"] == holds_before  # nothing was posted


# ----------------------------------------------------------- trust boundaries


def test_verifier_read_only_guard_blocks_a_mutating_capability(
    fresh: str, artifacts: dict[str, Path], tmp_path: Path
) -> None:
    """Point the verifier at the EXECUTOR artifact: the first business POST
    (the hold review) is aborted at the network layer, the run cannot
    proceed, and the report is void — with the violation named."""
    report = observe_in_process(
        artifacts["meridian_place_hold"], EXECUTOR_PARAMS, tmp_path / "runs", step_budget_s=3.0
    )
    assert report.ok is False and report.observation is None
    assert any("/hold/review" in v for v in report.read_only_violations)
    assert truth(fresh) == "OPEN"


def test_verifier_report_carries_no_balances_or_contact_data(
    fresh: str, artifacts: dict[str, Path], tmp_path: Path
) -> None:
    run, _ = twin(artifacts, tmp_path)
    pre_json = (Path(run.manifest.twin_dir) / "pre.json").read_text()
    assert "$" not in pre_json and "@example.com" not in pre_json
    assert run.pre is not None and run.pre.observation is not None
    assert {s.share_id for s in run.pre.observation.shares} == {"100234-S0001", SHARE}


def test_evidence_bundle_is_complete_and_every_chain_verifies(
    fresh: str, artifacts: dict[str, Path], tmp_path: Path
) -> None:
    run, _ = twin(artifacts, tmp_path, executor_chaos=ChaosMode.COMMIT_WITH_LOST_ACK)
    twin_dir = Path(run.manifest.twin_dir)
    for name in ("trace.jsonl", "expected.json", "pre.json", "post.json", "executor.json",
                 "reconciliation.json", "manifest.json", "twin.json"):
        assert (twin_dir / name).exists(), name
    assert all(run.manifest.trace_chain_intact.values()), run.manifest.trace_chain_intact
    for run_dir in run.manifest.run_dirs.values():
        assert run_dir is not None and verify_chain(Path(run_dir))
    # The executor's own trace records the refusal-to-claim and the evidence.
    events = [json.loads(ln) for ln in
              (Path(run.manifest.run_dirs["executor"] or "") / "trace.jsonl").read_text()
              .splitlines()]
    kinds = [e["event"] for e in events]
    assert "unresolved" in kinds and kinds[-1] == "run_finished"
    assert run.executor is not None
    report = run.executor.result["report"]
    assert isinstance(report, dict) and report["screenshot_path"]
    # The fault record is evidence ABOUT the experiment, not an input to the judgment.
    assert run.manifest.fault_mode == "COMMIT_WITH_LOST_ACK"
    rec = json.loads((twin_dir / "reconciliation.json").read_text())
    assert "chaos" not in json.dumps(rec).lower()


def test_verifier_and_twin_paths_import_no_model_code() -> None:
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys, hands.verifier, hands.twin, hands.reconcile, hands.chaos;"
         "bad = {'openai', 'hands.llm', 'hands.planner', 'hands.discover'} & set(sys.modules);"
         "assert not bad, bad"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
