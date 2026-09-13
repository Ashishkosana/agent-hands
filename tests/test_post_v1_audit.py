"""Regression coverage for the five post-V1 audit defects ported onto the V1
architecture (branch fix/post-v1-audit-port).

P1  API idempotency TOCTOU: same key concurrently -> one execution.
P2  Sealed audit trace tail: rewrite / truncate of the final record detected;
    sealed / unsealed / broken are distinct reported states.
P3  Step-scoped blocked-request attribution: an earlier harmless
    off-allowlist subresource does not relabel a later locator failure.
P4  Surface / Playwright infrastructure exceptions are contained inside the
    six-way contract (typed Failure, or UNRESOLVED when a hazard exists).
P5  Per-run attribution is reset before the risk gate can refuse a run.

Every test here is hermetic: the Fairview fixture app or a stub. No live
MERIDIAN operations.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from hands.api import create_api
from hands.artifact import Capability
from hands.cli import main as cli_main
from hands.replay import EngineConfig, ReplayEngine, _Hazard
from hands.results import Failure, PolicyViolation, Success, Unresolved
from hands.surface import PlaywrightError, SurfaceError, WebSurface
from hands.trace import TIP_FILE, Trace, chain_tip, is_sealed, verify_chain
from tests.conftest import _free_port, set_fault

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACT_DIR = REPO_ROOT / "capabilities" / "generated"


def engine(tmp_path: Path) -> ReplayEngine:
    return ReplayEngine(EngineConfig(runs_dir=tmp_path / "runs"))


# ------------------------------------------------------------------------ P1


class TestP1IdempotencyCriticalSection:
    def _client(self, calls: list[dict[str, str]], gate: threading.Event) -> Any:
        app = create_api(gen_dir=ARTIFACT_DIR, runs_dir=Path("runs"))
        app.config["TESTING"] = True

        class SlowStubEngine:
            last_run_dir = None

            def run(self, cap: Any, params: dict[str, str]) -> Success:
                calls.append(dict(params))
                # Hold the first execution open until the test says so, so
                # the second request is guaranteed to arrive mid-flight.
                gate.wait(timeout=10)
                return Success(outputs={"confirmation_number": f"CN{len(calls):04d}"})

        for cell in app.view_functions["invoke"].__closure__ or []:
            if type(cell.cell_contents).__name__ == "ReplayEngine":
                cell.cell_contents = SlowStubEngine()
        return app

    def test_p1_same_key_concurrently_executes_once_and_replays(self) -> None:
        """Two requests carrying the same idempotency key, in flight at the
        same time: exactly one execution; the other caller receives the first
        caller's envelope marked as a replay."""
        calls: list[dict[str, str]] = []
        gate = threading.Event()
        app = self._client(calls, gate)
        body = {
            "params": {
                "operator_id": "teller1",
                "member_number": "100987",
                "from_share": "MMKT-11",
                "to_share": "MMKT-5",
                "amount": "1",
            },
            "idempotency_key": "txn-concurrent-1",
        }
        responses: dict[str, Any] = {}

        def post(tag: str) -> None:
            with app.test_client() as client:
                resp = client.post("/capabilities/meridian_funds_transfer/invoke", json=body)
                responses[tag] = (resp.status_code, resp.get_json())

        first = threading.Thread(target=post, args=("first",))
        second = threading.Thread(target=post, args=("second",))
        first.start()
        # Deterministic ordering: the first request is inside engine.run
        # (holding the lock) before the second one is even sent.
        deadline = threading.Event()
        for _ in range(200):
            if calls:
                break
            deadline.wait(0.01)
        assert len(calls) == 1, "first request never reached the engine"
        second.start()
        # The second request must be blocked at the lock, not executing.
        second.join(timeout=0.5)
        assert second.is_alive(), "second caller did not wait for the first"
        assert len(calls) == 1
        gate.set()
        first.join(timeout=10)
        second.join(timeout=10)
        assert not first.is_alive() and not second.is_alive()

        assert len(calls) == 1, "the same key executed more than once"
        statuses = sorted(code for code, _ in responses.values())
        assert statuses == [200, 200]
        replays = [env for _, env in responses.values() if env.get("idempotent_replay")]
        originals = [env for _, env in responses.values() if not env.get("idempotent_replay")]
        assert len(replays) == 1 and len(originals) == 1
        assert replays[0]["result"]["outputs"] == originals[0]["result"]["outputs"]

    def test_p1_envelope_carries_the_audit_chain_tip(self, tmp_path: Path) -> None:
        """The audit anchor leaves the run directory in the envelope (P2)."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        with Trace(run_dir) as trace:
            trace.emit("run_started", capability="meridian_funds_transfer")
            trace.emit("run_finished", result={"result": "success"})
        app = create_api(gen_dir=ARTIFACT_DIR, runs_dir=tmp_path)
        app.config["TESTING"] = True

        class StubEngine:
            last_run_dir = run_dir

            def run(self, cap: Any, params: dict[str, str]) -> Success:
                return Success(outputs={"confirmation_number": "CN1"})

        for cell in app.view_functions["invoke"].__closure__ or []:
            if type(cell.cell_contents).__name__ == "ReplayEngine":
                cell.cell_contents = StubEngine()
        resp = app.test_client().post(
            "/capabilities/meridian_funds_transfer/invoke",
            json={
                "params": {
                    "operator_id": "teller1",
                    "member_number": "100987",
                    "from_share": "MMKT-11",
                    "to_share": "MMKT-5",
                    "amount": "1",
                }
            },
        )
        envelope = resp.get_json()
        assert envelope["audit_chain_tip"] == chain_tip(run_dir)
        assert isinstance(envelope["audit_chain_tip"], str) and envelope["audit_chain_tip"]
        assert verify_chain(run_dir, expected_tip=envelope["audit_chain_tip"])
        assert not verify_chain(run_dir, expected_tip="0" * 64)


# ------------------------------------------------------------------------ P2


def _write_run(tmp_path: Path, name: str = "run") -> Path:
    run_dir = tmp_path / name
    run_dir.mkdir()
    with Trace(run_dir) as trace:
        trace.emit("run_started", capability="demo", params={"member_id": "12345"})
        trace.emit("step_started", step="s1", intent="type", risk="safe")
        trace.emit("acted", step="s1", action="type")
        trace.emit("run_finished", result={"result": "failure"})
    return run_dir


class TestP2SealedTraceTail:
    def test_p2_valid_trace_is_sealed_and_verifies(self, tmp_path: Path) -> None:
        run_dir = _write_run(tmp_path)
        assert (run_dir / TIP_FILE).exists()
        assert is_sealed(run_dir)
        assert verify_chain(run_dir)
        seal = json.loads((run_dir / TIP_FILE).read_text())
        assert seal["records"] == 4
        assert seal["tip"] == chain_tip(run_dir)
        assert verify_chain(run_dir, expected_tip=seal["tip"])

    def test_p2_rewriting_the_final_run_finished_record_is_detected(self, tmp_path: Path) -> None:
        """The last record is pinned by no successor, so the chain alone
        cannot see it change. The seal can."""
        run_dir = _write_run(tmp_path)
        path = run_dir / "trace.jsonl"
        lines = path.read_text().splitlines()
        lines[-1] = lines[-1].replace('"failure"', '"success"')
        path.write_text("\n".join(lines) + "\n")
        assert not verify_chain(run_dir)
        # Without the seal the rewrite would have passed — that is the defect.
        (run_dir / TIP_FILE).unlink()
        assert verify_chain(run_dir)
        assert not is_sealed(run_dir)  # ...but the run is now reported UNSEALED

    def test_p2_truncating_the_tail_is_detected(self, tmp_path: Path) -> None:
        run_dir = _write_run(tmp_path)
        path = run_dir / "trace.jsonl"
        lines = path.read_text().splitlines()
        path.write_text("\n".join(lines[:-1]) + "\n")  # drop run_finished
        assert not verify_chain(run_dir)
        # The external anchor catches it even if the seal is deleted too.
        tip = chain_tip(run_dir)
        (run_dir / TIP_FILE).unlink()
        assert verify_chain(run_dir)  # chain-only check is blind to truncation
        assert not verify_chain(run_dir, expected_tip=tip)

    def test_p2_reopened_trace_reseals_at_the_new_tip(self, tmp_path: Path) -> None:
        run_dir = _write_run(tmp_path)
        old_tip = chain_tip(run_dir)
        with Trace(run_dir) as trace:
            trace.emit("postscript", note="appended after reopen")
        assert verify_chain(run_dir)
        assert chain_tip(run_dir) != old_tip
        assert json.loads((run_dir / TIP_FILE).read_text())["records"] == 5

    def test_p2_unsealed_v1_evidence_still_verifies_on_the_chain(self, tmp_path: Path) -> None:
        """Compatibility: V1 twin evidence bundles predate the seal. They
        verify on the chain and report unsealed — never broken."""
        run_dir = _write_run(tmp_path)
        (run_dir / TIP_FILE).unlink()
        assert verify_chain(run_dir)
        assert not is_sealed(run_dir)
        assert chain_tip(run_dir) is None

    def test_p2_committed_twin_evidence_is_unaffected(self) -> None:
        traces = sorted((REPO_ROOT / "evidence" / "twin").rglob("trace.jsonl"))
        assert traces, "V1 evidence bundles missing"
        for trace_file in traces:
            assert verify_chain(trace_file.parent), trace_file
            assert not is_sealed(trace_file.parent)  # predates the seal: unsealed, not broken

    def test_p2_hands_explain_distinguishes_sealed_unsealed_broken(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def explain(run_dir: Path, *extra: str) -> str:
            cli_main(
                [
                    "explain",
                    run_dir.name,
                    "--runs-dir",
                    str(run_dir.parent),
                    "--gen-dir",
                    str(ARTIFACT_DIR),
                    *extra,
                ]
            )
            return capsys.readouterr().out

        sealed = _write_run(tmp_path, "sealed")
        out = explain(sealed)
        assert "SEALED" in out and "UNSEALED" not in out and "BROKEN" not in out

        out = explain(sealed, "--expect-tip", str(chain_tip(sealed)))
        assert "RECONCILED" in out

        out = explain(sealed, "--expect-tip", "0" * 64)
        assert "BROKEN" in out

        unsealed = _write_run(tmp_path, "unsealed")
        (unsealed / TIP_FILE).unlink()
        out = explain(unsealed)
        assert "UNSEALED" in out and "BROKEN" not in out

        broken = _write_run(tmp_path, "broken")
        path = broken / "trace.jsonl"
        lines = path.read_text().splitlines()
        path.write_text("\n".join(lines[:-1]) + "\n")
        out = explain(broken)
        assert "BROKEN" in out

    def test_p2_dashboard_reports_three_log_states(self, tmp_path: Path) -> None:
        from dashboard.app import _summarize_run

        _write_run(tmp_path, "sealed")
        unsealed = _write_run(tmp_path, "unsealed")
        (unsealed / TIP_FILE).unlink()
        broken = _write_run(tmp_path, "broken")
        lines = (broken / "trace.jsonl").read_text().splitlines()
        (broken / "trace.jsonl").write_text("\n".join(lines[:-1]) + "\n")

        states = {name: (_summarize_run(tmp_path / name) or {})["log_state"] for name in
                  ("sealed", "unsealed", "broken")}
        assert states == {"sealed": "sealed", "unsealed": "unsealed", "broken": "broken"}


# ------------------------------------------------------------------------ P3


def test_p3_earlier_blocked_subresource_does_not_relabel_a_later_locator_failure(
    capability: Capability, fixture_app: str, tmp_path: Path
) -> None:
    """A harmless off-allowlist pixel is blocked while the entry page loads;
    later the search field's label has drifted. The failure is the locator
    failure — not a policy violation caused by the pixel."""
    set_fault(fixture_app, "external_asset", True)
    set_fault(fixture_app, "renamed_label", True)
    eng = engine(tmp_path)
    result = eng.run(capability, {"member_id": "12345"})
    assert isinstance(result, Failure), result
    assert result.report.step_id == "s1"
    assert "Member ID" in result.report.expected or "label" in result.report.observed.lower()
    # The block itself is still in the audit trail — attributed, not hidden.
    assert eng.last_run_dir is not None
    events = [json.loads(ln) for ln in (eng.last_run_dir / "trace.jsonl").read_text().splitlines()]
    blocked = next(e for e in events if e["event"] == "policy_blocked_requests")
    assert any("localhost:9" in url for url in blocked["urls"])


def test_p3_blocked_traffic_during_the_failing_step_is_still_a_policy_violation(
    capability: Capability, tmp_path: Path
) -> None:
    """The existing behaviour, narrowed: a step whose own traffic is blocked
    is a policy violation (this is test_policy's case, re-asserted against the
    step-scoped mark)."""
    from hands.artifact import NavigateAction, Step, UrlMatches

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
    assert "while this step ran" in result.detail


def test_p3_hazard_still_wins_over_blocked_request_attribution(
    capability: Capability, fixture_app: str, tmp_path: Path
) -> None:
    """P3 must never override V1: with a consequential hazard on the run, a
    failure with blocked traffic in the failing step is UNRESOLVED."""
    set_fault(fixture_app, "renamed_label", True)
    set_fault(fixture_app, "external_asset", True)
    eng = engine(tmp_path)
    original_run_step = eng._run_step

    def poisoned(*args: Any, **kwargs: Any) -> Any:
        # Simulate: a risky step earlier in this run dispatched a mutation.
        eng._hazard = _Hazard(step_id="sX", intent="place hold", requests=("POST /hold",))
        return original_run_step(*args, **kwargs)

    eng._run_step = poisoned  # type: ignore[method-assign]
    result = eng.run(capability, {"member_id": "12345"})
    assert isinstance(result, Unresolved), result
    assert result.step_id == "sX"


# ------------------------------------------------------------------------ P4


def test_p4_unreachable_entry_url_is_a_typed_failure(
    capability: Capability, tmp_path: Path
) -> None:
    """Entry navigation to a closed port must not escape run() as a raw
    Playwright exception; it is a Failure with step_id=None and a
    surface_unavailable trace record."""
    port = _free_port()  # bound-then-released: nothing is listening there
    capability.target.entry.web.url = f"http://127.0.0.1:{port}/"
    eng = engine(tmp_path)
    result = eng.run(capability, {"member_id": "12345"})
    assert isinstance(result, Failure), result
    assert result.report.step_id is None
    assert f"127.0.0.1:{port}" in result.report.expected
    assert "ERR_CONNECTION_REFUSED" in result.report.observed
    assert eng.last_run_dir is not None
    events = [json.loads(ln) for ln in (eng.last_run_dir / "trace.jsonl").read_text().splitlines()]
    assert any(e["event"] == "surface_unavailable" for e in events)
    assert events[-1]["event"] == "run_finished"
    assert events[-1]["result"]["result"] == "failure"
    assert verify_chain(eng.last_run_dir) and is_sealed(eng.last_run_dir)


def test_p4_early_surface_failure_before_launch_is_contained(
    capability: Capability, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The browser never launches. blocked_requests / dispatched exist before
    start(), stop() tolerates a never-started surface, and the caller gets a
    typed Failure."""

    def broken_start(self: WebSurface, *args: Any, **kwargs: Any) -> None:
        raise PlaywrightError("Executable doesn't exist at /nowhere/chromium")

    monkeypatch.setattr(WebSurface, "start", broken_start)
    fresh = WebSurface()
    assert fresh.blocked_requests == [] and fresh.dispatched == []
    fresh.stop()  # must not raise on a surface that never started

    eng = engine(tmp_path)
    result = eng.run(capability, {"member_id": "12345"})
    assert isinstance(result, Failure), result
    assert result.report.step_id is None
    assert "Executable doesn't exist" in result.report.observed
    assert result.report.screenshot_path is None  # nothing to capture


def test_p4_surface_error_mid_run_is_contained(
    capability: Capability, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(self: ReplayEngine, *args: Any, **kwargs: Any) -> Any:
        raise SurfaceError("browser context was closed underneath the engine")

    monkeypatch.setattr(ReplayEngine, "_execute", explode)
    result = engine(tmp_path).run(capability, {"member_id": "12345"})
    assert isinstance(result, Failure), result
    assert result.report.step_id is None
    assert "closed underneath" in result.report.observed


def test_p4_infrastructure_failure_after_a_consequential_dispatch_is_unresolved(
    capability: Capability, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V1 invariant: consequential mutation dispatched + later infrastructure
    failure => UNRESOLVED, never Failure."""

    def dispatch_then_die(self: ReplayEngine, *args: Any, **kwargs: Any) -> Any:
        self._hazard = _Hazard(step_id="s7", intent="place hold", requests=("POST /hold",))
        raise PlaywrightError("Target page, context or browser has been closed")

    monkeypatch.setattr(ReplayEngine, "_execute", dispatch_then_die)
    eng = engine(tmp_path)
    result = eng.run(capability, {"member_id": "12345"})
    assert isinstance(result, Unresolved), result
    assert result.step_id == "s7" and result.dispatched is True
    assert result.report.step_id is None  # the failure that left it in doubt
    assert eng.last_run_dir is not None
    events = [json.loads(ln) for ln in (eng.last_run_dir / "trace.jsonl").read_text().splitlines()]
    assert any(e["event"] == "unresolved" for e in events)
    assert not any(e["event"] == "surface_unavailable" for e in events)


def test_p4_entry_navigation_blocked_by_the_allowlist_is_a_policy_violation(
    capability: Capability, tmp_path: Path
) -> None:
    """The entry URL itself is off the allowlist: the aborted navigation is
    reported as the policy violation it is, not as a mysterious failure."""
    config = EngineConfig(runs_dir=tmp_path / "runs")
    config.policy.allowed_hosts = ["allowed.invalid"]
    result = ReplayEngine(config).run(capability, {"member_id": "12345"})
    assert isinstance(result, PolicyViolation), result
    assert result.rule == "off_allowlist_traffic"
    assert "session could not be established" in result.detail


# ------------------------------------------------------------------------ P5


def test_p5_refused_run_does_not_inherit_the_previous_runs_directory(
    capability: Capability, tmp_path: Path
) -> None:
    """Run A creates evidence; run B is refused by the risk gate before any
    trace exists. B must not point at A's directory."""
    eng = engine(tmp_path)
    first = eng.run(capability, {"member_id": "12345"})
    assert isinstance(first, Success)
    run_a = eng.last_run_dir
    assert run_a is not None and (run_a / "trace.jsonl").exists()

    risky = capability.model_copy(deep=True)
    risky.steps[1].risk = "risky"
    second = eng.run(risky, {"member_id": "12345"})
    assert isinstance(second, PolicyViolation)
    assert second.rule == "unattended_risky_requires_review"
    assert eng.last_run_dir is None
    assert eng._hazard is None


def test_p5_validation_refusal_also_clears_attribution(
    capability: Capability, tmp_path: Path
) -> None:
    eng = engine(tmp_path)
    assert isinstance(eng.run(capability, {"member_id": "12345"}), Success)
    assert eng.last_run_dir is not None
    with pytest.raises(ValueError):
        eng.run(capability, {})  # missing required param: refused before any trace
    assert eng.last_run_dir is None


def test_p5_reset_does_not_erase_state_the_current_run_established(
    capability: Capability, tmp_path: Path
) -> None:
    """The reset happens at the top of run(); the run then legitimately sets
    last_run_dir and it must survive to the caller."""
    eng = engine(tmp_path)
    eng.last_run_dir = tmp_path / "stale"
    result = eng.run(capability, {"member_id": "12345"})
    assert isinstance(result, Success)
    assert eng.last_run_dir is not None and eng.last_run_dir != tmp_path / "stale"
    assert eng.last_run_dir.parent == tmp_path / "runs"
