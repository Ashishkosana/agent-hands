"""Verified run orchestration: pre-image -> execute -> post-image -> reconcile.

This module sequences the three authorities and writes the evidence bundle.
It holds no judgment of its own: the verifier observes, the reconciler
decides, and the orchestrator refuses to act when the pre-image cannot be
established or already shows the target state (fail closed, and never
"verify" an effect that predates the run).

V1 boundary, on purpose:
- one executor capability (a consequential action) + one read-only verifier
  capability (the same record, observed independently);
- four verdicts; no DUPLICATE_DETECTED, no settlement/PENDING loops;
- NO automatic retry. ``retry_eligible`` is recorded, never acted on.

Evidence bundle (one directory per verified run, under runs/):
    trace.jsonl              the twin's own hash-chained event log
    expected.json            the effect contract judged against
    pre.json / post.json     the verifier reports (observations + run dirs)
    executor.json            the executor's masked result + run dir
    reconciliation.json      the verdict, reason, inputs hash
    manifest.json            artifact hashes, every trace's SHA-256 and chain
                             verification, fault-injection record, durations
Traces are tamper-EVIDENT (hash-chained, keyless) — see hands.trace for the
exact limits; nothing here adds cryptographic non-repudiation.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel

from hands.artifact import (
    Capability,
    NavigateAction,
    dump_capability,
    load_capability,
    sign_risk_review,
)
from hands.chaos import ChaosMode, FaultInjector
from hands.reconcile import ExecutorClaim, ExpectedEffect, Reconciliation, Verdict, reconcile
from hands.replay import EngineConfig, ReplayEngine, masked_result
from hands.results import ReplayResult
from hands.trace import Trace, is_chained, new_run_dir, verify_chain
from hands.verifier import VerifierReport, VerifierRunner


@dataclass
class TwinConfig:
    executor_artifact: Path
    verifier_artifact: Path
    executor_params: dict[str, str]  # includes the sensitive password
    verifier_params: dict[str, str]  # may sign on as a different operator
    expected: ExpectedEffect
    runs_dir: Path
    executor_chaos: FaultInjector | None = None  # test/eval only
    verifier_chaos: ChaosMode | None = None  # test/eval only; applied to the POST-image read
    step_budget_s: float | None = None
    verifier_python: str | None = None


class ExecutorRecord(BaseModel):
    result_kind: str
    result: dict[str, object]
    run_dir: str | None
    artifact_sha256: str
    elapsed_s: float


class Manifest(BaseModel):
    twin_dir: str
    fault_mode: str | None
    chaos_fired: list[dict[str, object]]
    executor_artifact_sha256: str
    verifier_artifact_sha256: str
    run_dirs: dict[str, str | None]
    trace_sha256: dict[str, str | None]
    trace_chain_intact: dict[str, bool | None]
    durations_s: dict[str, float]


class TwinRun(BaseModel):
    twin_id: str
    started_at: str
    fault_mode: str | None
    expected: ExpectedEffect
    executed: bool
    aborted: str | None  # set when the orchestrator refused to execute
    pre: VerifierReport | None
    executor: ExecutorRecord | None
    post: VerifierReport | None
    reconciliation: Reconciliation | None
    manifest: Manifest


def _sha256_file(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_hash(cap: Capability) -> str:
    return hashlib.sha256(dump_capability(cap).encode()).hexdigest()


def _write(path: Path, model: BaseModel | dict[str, object]) -> None:
    data = model.model_dump(mode="json") if isinstance(model, BaseModel) else model
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")


def run_twin(cfg: TwinConfig) -> TwinRun:
    executor_cap = load_capability(cfg.executor_artifact)
    verifier_cap = load_capability(cfg.verifier_artifact)
    twin_dir = new_run_dir(cfg.runs_dir, "twin")
    started_at = datetime.now(tz=UTC).isoformat(timespec="milliseconds")
    durations: dict[str, float] = {}
    fault_mode = (
        cfg.executor_chaos.mode.value if cfg.executor_chaos is not None
        else (cfg.verifier_chaos.value if cfg.verifier_chaos is not None else None)
    )
    verifier = VerifierRunner(
        cfg.verifier_artifact, cfg.verifier_params, cfg.runs_dir,
        step_budget_s=cfg.step_budget_s,
        python=cfg.verifier_python or sys.executable,
    )
    _write(twin_dir / "expected.json", cfg.expected)

    pre: VerifierReport | None = None
    post: VerifierReport | None = None
    executor: ExecutorRecord | None = None
    reconciliation: Reconciliation | None = None
    aborted: str | None = None
    executed = False
    result: ReplayResult | None = None

    with Trace(twin_dir) as trace:
        trace.emit(
            "twin_started",
            expected=cfg.expected.model_dump(mode="json"),
            executor_artifact_sha256=_artifact_hash(executor_cap),
            verifier_artifact_sha256=_artifact_hash(verifier_cap),
            fault_mode=fault_mode,
            executor_operator=cfg.executor_params.get("operator_id"),
            verifier_operator=cfg.verifier_params.get("operator_id"),
        )

        # 1. Pre-image: refuse to act on a state we cannot read, or one that
        #    already shows the target — there would be nothing to attribute.
        t0 = time.monotonic()
        pre = verifier.observe()
        durations["pre_image"] = time.monotonic() - t0
        _write(twin_dir / "pre.json", pre)
        target_pre = pre.observation.status_of(cfg.expected.share_id) if pre.observation else None
        trace.emit("pre_image", ok=pre.ok, target_status=target_pre, run_dir=pre.run_dir,
                   error=pre.error)
        if not pre.ok:
            aborted = f"pre-image unavailable ({pre.error}); execution refused"
        elif target_pre != cfg.expected.from_status:
            aborted = (f"pre-image shows {cfg.expected.share_id} = {target_pre!r}, not "
                       f"{cfg.expected.from_status!r}; execution refused")
        else:
            # 2. Execute the consequential action. Chaos, if any, is installed
            #    on THIS surface only.
            config = EngineConfig(runs_dir=cfg.runs_dir, interceptor=cfg.executor_chaos)
            if cfg.step_budget_s is not None:
                config.step_budget_s = cfg.step_budget_s
            engine = ReplayEngine(config)
            t0 = time.monotonic()
            result = engine.run(executor_cap, cfg.executor_params)
            durations["executor"] = time.monotonic() - t0
            executed = True
            executor = ExecutorRecord(
                result_kind=result.result,
                result=masked_result(executor_cap, result),
                run_dir=str(engine.last_run_dir) if engine.last_run_dir else None,
                artifact_sha256=_artifact_hash(executor_cap),
                elapsed_s=durations["executor"],
            )
            _write(twin_dir / "executor.json", executor)
            trace.emit("executor_finished", result_kind=result.result, run_dir=executor.run_dir,
                       dispatched=getattr(result, "dispatched", None))

            # 3. Post-image: a fresh verifier, again. Never the executor's page.
            t0 = time.monotonic()
            post = verifier.observe(chaos=cfg.verifier_chaos)
            durations["post_image"] = time.monotonic() - t0
            _write(twin_dir / "post.json", post)
            target_post = (
                post.observation.status_of(cfg.expected.share_id) if post.observation else None
            )
            trace.emit("post_image", ok=post.ok, target_status=target_post, run_dir=post.run_dir,
                       error=post.error)

            # 4. Judge. Pure function; its inputs are on disk beside its output.
            reconciliation = reconcile(
                cfg.expected, pre.observation, post.observation, ExecutorClaim.from_result(result)
            )
        if aborted is not None:
            trace.emit("execution_refused", reason=aborted)
        if reconciliation is not None:
            _write(twin_dir / "reconciliation.json", reconciliation)
            trace.emit(
                "reconciled",
                verdict=reconciliation.verdict.value,
                reason=reconciliation.reason,
                retry_eligible=reconciliation.retry_eligible,
                collateral_changes=reconciliation.collateral_changes,
                inputs_sha256=reconciliation.inputs_sha256,
            )
        trace.emit("twin_finished", executed=executed, aborted=aborted)

    run_dirs = {
        "twin": str(twin_dir),
        "pre_verifier": pre.run_dir if pre else None,
        "executor": executor.run_dir if executor else None,
        "post_verifier": post.run_dir if post else None,
    }
    trace_paths = {k: (Path(v) / "trace.jsonl" if v else None) for k, v in run_dirs.items()}
    manifest = Manifest(
        twin_dir=str(twin_dir),
        fault_mode=fault_mode,
        chaos_fired=list(cfg.executor_chaos.fired) if cfg.executor_chaos else [],
        executor_artifact_sha256=_artifact_hash(executor_cap),
        verifier_artifact_sha256=_artifact_hash(verifier_cap),
        run_dirs=run_dirs,
        trace_sha256={k: _sha256_file(p) for k, p in trace_paths.items()},
        trace_chain_intact={
            k: (verify_chain(Path(v)) if v and is_chained(Path(v)) else None)
            for k, v in run_dirs.items()
        },
        durations_s={k: round(v, 3) for k, v in durations.items()},
    )
    _write(twin_dir / "manifest.json", manifest)
    run = TwinRun(
        twin_id=twin_dir.name,
        started_at=started_at,
        fault_mode=fault_mode,
        expected=cfg.expected,
        executed=executed,
        aborted=aborted,
        pre=pre,
        executor=executor,
        post=post,
        reconciliation=reconciliation,
        manifest=manifest,
    )
    _write(twin_dir / "twin.json", run)
    return run


def verdict_of(run: TwinRun) -> str:
    """The one-word outcome of a verified run, for tables and tests. A run
    the orchestrator refused to execute (unreadable or already-in-target-state
    pre-image) is REFUSED: nothing happened, so there is nothing to verify."""
    if run.reconciliation is not None:
        return run.reconciliation.verdict.value
    return "REFUSED" if run.aborted else Verdict.UNVERIFIABLE.value


# --------------------------------------------------------------------- harness


def retarget(cap: Capability, base_url: str, reviewer: str) -> Capability:
    """Point an artifact recorded against one origin at another (the local
    fixture), rewriting the entry URL and any recorded navigate URLs, then
    RE-SIGN it — changing the target voids the original risk review by
    design, and the harness signs for itself rather than pretending the
    original approval still applies."""
    origin = urlparse(cap.target.entry.web.url)
    new = urlparse(base_url)

    def swap(url: str) -> str:
        parsed = urlparse(url)
        if parsed.netloc == origin.netloc:
            return parsed._replace(scheme=new.scheme, netloc=new.netloc).geturl()
        return url

    doc = cap.model_dump(mode="json", by_alias=True)
    doc["target"]["entry"]["web"]["url"] = swap(cap.target.entry.web.url)
    for cond in doc.get("conditions", []):
        for step in cond.get("recovery", []):
            if step["action"]["kind"] == "navigate":
                step["action"]["url"] = swap(step["action"]["url"])
    for step in doc["steps"]:
        if step["action"]["kind"] == "navigate":
            step["action"]["url"] = swap(step["action"]["url"])
    doc["risk_review"] = {"reviewed_by": None, "artifact_hash": None}
    retargeted = Capability.model_validate(doc)
    assert all(
        not isinstance(s.action, NavigateAction) or urlparse(s.action.url).netloc == new.netloc
        for s in retargeted.steps
    )
    return sign_risk_review(retargeted, reviewer)
