"""Transfer Intent Gate: per-run approval bound into the hash-chained trace.

Recipe-level risk_review is not this-run intent. These tests prove the gate
fails closed (unattended / deny / TTL / hash mismatch / dual-control) and
that an approval binds operator + intent_hash without consulting a model.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from hands.artifact import Capability, load_capability, sign_risk_review
from hands.cli import main
from hands.intent import (
    IntentHashMismatch,
    build_intent_payload,
    compute_intent_hash,
    intent_approval_required,
    intent_prompt,
    is_money_moving_step,
    verify_intent_hash,
)
from hands.replay import EngineConfig, EscalationSettings, PolicySettings, ReplayEngine
from hands.results import Failure, PolicyViolation, Success
from hands.trace import Trace, verify_chain
from tests.test_escalation import Operator

REPO = Path(__file__).resolve().parent.parent
MERIDIAN = REPO / "capabilities" / "generated" / "meridian_funds_transfer.json"
LOOKUP = REPO / "capabilities" / "lookup_member_balance.json"


def _as_money_moving(capability: Capability) -> Capability:
    """Relabel the Fairview search-submit as a Post Transfer so the gate can
    be exercised against the live fixture without MERIDIAN or a new UI."""
    capability.steps[1].intent = "Post the transfer"
    capability.steps[1].risk = "risky"
    return capability


def _gate_engine(
    tmp_path: Path,
    *,
    attended: bool = True,
    ttl_s: float = 30.0,
    dual: bool = False,
    invoker: str | None = None,
) -> ReplayEngine:
    return ReplayEngine(
        EngineConfig(
            runs_dir=tmp_path / "runs",
            escalation=EscalationSettings(ttl_s=ttl_s, console_port=0) if attended else None,
            policy=PolicySettings(
                require_intent_approval=True,
                require_intent_dual_control=dual,
                invoker=invoker,
            ),
        )
    )


def _events(tmp_path: Path) -> list[dict[str, object]]:
    (run_dir,) = (tmp_path / "runs").iterdir()
    return [
        json.loads(line) for line in (run_dir / "trace.jsonl").read_text().splitlines()
    ]


class TestIntentHash:
    def test_canonical_json_is_key_order_independent(self) -> None:
        cap = load_capability(MERIDIAN)
        params = {
            "operator_id": "teller1",
            "password": "secret",
            "member_number": "100234",
            "from_share": "S0001-1",
            "to_share": "S0001-3",
            "amount": "1.00",
        }
        a = build_intent_payload(
            run_id="run-a",
            capability=cap,
            artifact_sha256="abc",
            params=params,
            step_id="s13",
        )
        b = build_intent_payload(
            run_id="run-a",
            capability=cap,
            artifact_sha256="abc",
            params=params,
            step_id="s13",
        )
        assert compute_intent_hash(a) == compute_intent_hash(b)
        assert "password" not in a["params"]
        assert a["member_number"] == "100234"

    def test_mismatch_refuses(self) -> None:
        cap = load_capability(MERIDIAN)
        params = {
            "operator_id": "teller1",
            "password": "secret",
            "member_number": "100234",
            "from_share": "S0001-1",
            "to_share": "S0001-3",
            "amount": "1.00",
        }
        payload = build_intent_payload(
            run_id="run-a",
            capability=cap,
            artifact_sha256="abc",
            params=params,
            step_id="s13",
        )
        expected = compute_intent_hash(payload)
        verify_intent_hash(payload, expected)  # matching is fine
        tampered = {**payload, "amount": "9999.00"}
        with pytest.raises(IntentHashMismatch, match="intent_hash mismatch"):
            verify_intent_hash(tampered, expected)

    def test_prompt_is_plain_language(self) -> None:
        prompt = intent_prompt(
            {
                "amount": "25.50",
                "from_share": "Checking",
                "to_share": "Savings",
                "member_number": "100234",
            }
        )
        assert prompt == "Approve transfer of $25.50 from Checking → Savings for member 100234?"


class TestGateDetection:
    def test_meridian_post_transfer_is_money_moving(self) -> None:
        cap = load_capability(MERIDIAN)
        post = next(s for s in cap.steps if s.id == "s13")
        sign_on = next(s for s in cap.steps if s.id == "s3")
        assert is_money_moving_step(post)
        assert not is_money_moving_step(sign_on)
        assert intent_approval_required(cap, None) is True
        assert intent_approval_required(cap, False) is False

    def test_lookup_is_off_by_default(self) -> None:
        cap = load_capability(LOOKUP)
        assert intent_approval_required(cap, None) is False
        assert intent_approval_required(cap, True) is False  # no money-moving step


def test_unattended_unsigned_intent_cannot_pass_money_step(
    capability: Capability, tmp_path: Path
) -> None:
    """Signed recipe + money-moving step + no console → PolicyViolation.
    The browser is never launched; the post never happens."""
    _as_money_moving(capability)
    signed = sign_risk_review(capability, "reviewer-1")
    result = _gate_engine(tmp_path, attended=False).run(signed, {"member_id": "12345"})
    assert isinstance(result, PolicyViolation)
    assert result.rule == "intent_approval_required"
    assert not (tmp_path / "runs").exists() or not any((tmp_path / "runs").iterdir())


def test_approve_binds_operator_and_hash_into_chain(
    capability: Capability, fixture_app: str, tmp_path: Path
) -> None:
    _as_money_moving(capability)
    operator = Operator(
        tmp_path / "runs",
        [{"do": "await", "state": "paused"}, {"do": "approve"}],
    )
    operator.start()
    result = _gate_engine(tmp_path).run(capability, {"member_id": "12345"})
    operator.join()
    assert isinstance(result, Success)
    assert result.outputs == {"savings_balance": Decimal("1234.50")}

    events = _events(tmp_path)
    kinds = [e["event"] for e in events]
    assert "intent_approval_raised" in kinds
    assert "intent_approved" in kinds
    approved = next(e for e in events if e["event"] == "intent_approved")
    raised = next(e for e in events if e["event"] == "intent_approval_raised")
    assert approved["operator"] == "teller7"
    assert approved["intent_hash"] == raised["intent_hash"]
    assert len(str(approved["intent_hash"])) == 64
    # The money step acted only AFTER approval.
    acted_idx = next(
        i for i, e in enumerate(events) if e["event"] == "acted" and e.get("step") == "s2"
    )
    approved_idx = next(i for i, e in enumerate(events) if e["event"] == "intent_approved")
    assert approved_idx < acted_idx
    (run_dir,) = (tmp_path / "runs").iterdir()
    assert verify_chain(run_dir)
    payload = json.loads((run_dir / "intent-approval.json").read_text())
    assert payload["kind"] == "intent_approval"
    assert payload["intent_hash"] == approved["intent_hash"]


def test_deny_fails_closed_without_acting(
    capability: Capability, fixture_app: str, tmp_path: Path
) -> None:
    _as_money_moving(capability)
    operator = Operator(
        tmp_path / "runs",
        [{"do": "await", "state": "paused"}, {"do": "deny"}],
    )
    operator.start()
    result = _gate_engine(tmp_path).run(capability, {"member_id": "12345"})
    operator.join()
    assert isinstance(result, Failure)
    assert "denied" in result.report.observed
    events = _events(tmp_path)
    assert "intent_denied" in [e["event"] for e in events]
    assert not any(e["event"] == "acted" and e.get("step") == "s2" for e in events)


def test_ttl_fails_closed_without_acting(
    capability: Capability, fixture_app: str, tmp_path: Path
) -> None:
    _as_money_moving(capability)
    result = _gate_engine(tmp_path, ttl_s=1.0).run(capability, {"member_id": "12345"})
    assert isinstance(result, Failure)
    assert "not posted" in result.report.observed
    events = _events(tmp_path)
    assert "intent_approval_ttl_expired" in [e["event"] for e in events]
    assert not any(e["event"] == "acted" and e.get("step") == "s2" for e in events)


def test_dual_control_refuses_same_identity(
    capability: Capability, fixture_app: str, tmp_path: Path
) -> None:
    """Operator posts as teller7 (test Operator); invoker is the same name."""
    _as_money_moving(capability)
    operator = Operator(
        tmp_path / "runs",
        [{"do": "await", "state": "paused"}, {"do": "approve"}],
    )
    operator.start()
    result = _gate_engine(tmp_path, dual=True, invoker="teller7").run(
        capability, {"member_id": "12345"}
    )
    operator.join()
    assert isinstance(result, PolicyViolation)
    assert result.rule == "intent_dual_control"
    events = _events(tmp_path)
    assert "intent_dual_control_refused" in [e["event"] for e in events]
    assert not any(e["event"] == "acted" and e.get("step") == "s2" for e in events)


def test_dual_control_allows_distinct_operator(
    capability: Capability, fixture_app: str, tmp_path: Path
) -> None:
    _as_money_moving(capability)
    operator = Operator(
        tmp_path / "runs",
        [{"do": "await", "state": "paused"}, {"do": "approve"}],
    )
    operator.start()
    result = _gate_engine(tmp_path, dual=True, invoker="alice").run(
        capability, {"member_id": "12345"}
    )
    operator.join()
    assert isinstance(result, Success)


def test_explain_receipt_includes_intent_section(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "demo-intent-run"
    run_dir.mkdir(parents=True)
    with Trace(run_dir) as trace:
        trace.emit(
            "run_started",
            capability="meridian_funds_transfer",
            version=1,
            artifact_sha256="deadbeef",
            params={"member_number": "100234", "amount": "1"},
        )
        trace.emit(
            "intent_approval_raised",
            step="s13",
            intent_hash="a" * 64,
            prompt="Approve transfer of $1 from A → B for member 100234?",
        )
        trace.emit("intent_approved", operator="supervisor1", intent_hash="a" * 64)
        trace.emit("run_finished", result={"result": "success"})
    assert verify_chain(run_dir)

    import sys
    from io import StringIO

    buf = StringIO()
    old = sys.stdout
    try:
        sys.stdout = buf
        code = main(
            ["explain", "demo-intent-run", "--runs-dir", str(tmp_path / "runs")]
        )
    finally:
        sys.stdout = old
    assert code == 0
    receipt = buf.getvalue()
    assert "Transfer intent approved by 'supervisor1'" in receipt
    assert "intent_hash=" + "a" * 64 in receipt
    assert "chain intact" in receipt
    assert "model events=0" in receipt
    assert "NO model in the decision loop" in receipt
