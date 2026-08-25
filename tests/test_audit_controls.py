"""Audit controls: the tamper-evident trace chain and the maker-checker gate.

These are the examiner-facing guarantees: (1) any edit, removal, insertion, or
reordering of trace records is detectable by recomputing the hash chain, and
(2) the operator who recorded a risky capability cannot approve their own
steps — a different named reviewer must sign (four-eyes).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hands.artifact import (
    RiskReview,
    load_capability,
    risk_review_valid,
    sign_risk_review,
)
from hands.trace import TIP_FILE, Trace, chain_tip, is_chained, is_sealed, verify_chain

REPO = Path(__file__).resolve().parent.parent
ARTIFACT = REPO / "capabilities" / "lookup_member_balance.json"


def _write_run(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with Trace(run_dir) as trace:
        trace.emit("run_started", capability="demo", params={"member_id": "12345"})
        trace.emit("step_started", step="s1", intent="type", risk="safe")
        trace.emit("acted", step="s1", action="type")
        trace.emit("run_finished", result={"result": "success"})
    return run_dir


class TestTraceChain:
    def test_intact_chain_verifies(self, tmp_path: Path) -> None:
        run_dir = _write_run(tmp_path)
        assert is_chained(run_dir)
        assert verify_chain(run_dir)

    def test_edited_record_breaks_the_chain(self, tmp_path: Path) -> None:
        run_dir = _write_run(tmp_path)
        path = run_dir / "trace.jsonl"
        lines = path.read_text().splitlines()
        # An auditor's nightmare: quietly rewriting what step s1 did.
        lines[2] = lines[2].replace('"action": "type"', '"action": "click"')
        path.write_text("\n".join(lines) + "\n")
        assert not verify_chain(run_dir)

    def test_removed_record_breaks_the_chain(self, tmp_path: Path) -> None:
        run_dir = _write_run(tmp_path)
        path = run_dir / "trace.jsonl"
        lines = path.read_text().splitlines()
        del lines[1]  # drop a mid-log record
        path.write_text("\n".join(lines) + "\n")
        assert not verify_chain(run_dir)

    def test_reordered_records_break_the_chain(self, tmp_path: Path) -> None:
        run_dir = _write_run(tmp_path)
        path = run_dir / "trace.jsonl"
        lines = path.read_text().splitlines()
        lines[1], lines[2] = lines[2], lines[1]
        path.write_text("\n".join(lines) + "\n")
        assert not verify_chain(run_dir)

    def test_rewritten_final_record_breaks_the_seal(self, tmp_path: Path) -> None:
        """The chain alone cannot protect its own end: every record's hash is
        carried by its SUCCESSOR, so the last record — the one holding the
        run's RESULT — is pinned by nothing. Turning a failure into a success
        here is the cheapest and most valuable forgery available, so the seal
        has to catch it."""
        run_dir = _write_run(tmp_path)
        path = run_dir / "trace.jsonl"
        lines = path.read_text().splitlines()
        forged = json.loads(lines[-1])
        forged["result"] = {"result": "success", "outputs": {"savings_balance": "999999.00"}}
        lines[-1] = json.dumps(forged)  # seq and prev left untouched
        path.write_text("\n".join(lines) + "\n")
        assert not verify_chain(run_dir)

    def test_truncated_tail_breaks_the_seal(self, tmp_path: Path) -> None:
        """Dropping records off the end deletes its own evidence — unless the
        record count is anchored outside the file."""
        run_dir = _write_run(tmp_path)
        path = run_dir / "trace.jsonl"
        lines = path.read_text().splitlines()
        path.write_text("\n".join(lines[:2]) + "\n")
        assert not verify_chain(run_dir)

    def test_seal_travels_out_of_the_directory_it_protects(self, tmp_path: Path) -> None:
        """The tip is the anchor a caller keeps: it is what makes the seal
        worth more than another line in the same file."""
        run_dir = _write_run(tmp_path)
        tip = chain_tip(run_dir)
        assert tip is not None and len(tip) == 64  # a sha256 hex digest
        assert chain_tip(tmp_path / "no-such-run") is None

    def test_deleting_the_seal_is_a_reported_state_not_silent_intactness(
        self, tmp_path: Path
    ) -> None:
        """The seal sits in the SAME directory as the log, so anyone who can
        rewrite the log can delete it. Verification then degrades to the weaker
        chain-only check — which is only acceptable because the degradation is
        VISIBLE. `is_sealed` is what makes it visible; without it, a deleted
        seal renders identically to an intact run."""
        run_dir = _write_run(tmp_path)
        assert is_sealed(run_dir)
        (run_dir / TIP_FILE).unlink()
        assert not is_sealed(run_dir)
        assert verify_chain(run_dir)  # chain alone still passes — hence the state

    def test_kept_receipt_catches_the_forgery_the_seal_cannot(
        self, tmp_path: Path
    ) -> None:
        """Rewrite the final record AND delete the seal — both in-directory
        defences defeated at once. Only an anchor held somewhere else can
        catch this, which is the entire reason the tip travels in the invoke
        envelope."""
        run_dir = _write_run(tmp_path)
        receipt_tip = chain_tip(run_dir)
        assert receipt_tip is not None

        path = run_dir / "trace.jsonl"
        lines = path.read_text().splitlines()
        forged = json.loads(lines[-1])
        forged["result"] = {"result": "success", "outputs": {"savings_balance": "999999.00"}}
        lines[-1] = json.dumps(forged)
        path.write_text("\n".join(lines) + "\n")
        (run_dir / TIP_FILE).unlink()

        assert verify_chain(run_dir) is True  # in-directory checks: fully defeated
        assert verify_chain(run_dir, expected_tip=receipt_tip) is False  # the anchor holds

    def test_corrupt_seal_degrades_instead_of_exploding(self, tmp_path: Path) -> None:
        """A non-UTF-8 seal must not raise: `verify_chain` is called by the
        dashboard on every run it renders, so an unreadable sibling file would
        take the page down instead of reporting a state."""
        run_dir = _write_run(tmp_path)
        (run_dir / TIP_FILE).write_bytes(b"\xff\xfe not utf-8 at all")
        assert chain_tip(run_dir) is None
        assert is_sealed(run_dir) is False
        assert verify_chain(run_dir) is True  # reported as unsealed, not as broken

    def test_reopened_trace_continues_the_chain(self, tmp_path: Path) -> None:
        run_dir = _write_run(tmp_path)
        with Trace(run_dir) as trace:  # e.g. a second phase appending to the run
            trace.emit("postscript", note="appended after reopen")
        assert verify_chain(run_dir)

    def test_legacy_trace_is_unchained_not_tampered(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "legacy"
        run_dir.mkdir()
        (run_dir / "trace.jsonl").write_text(
            json.dumps({"ts": "2026-01-01T00:00:00.000+00:00", "event": "run_started"}) + "\n"
        )
        assert not is_chained(run_dir)  # distinguishable from a broken chain


class TestMakerChecker:
    def test_author_cannot_sign_their_own_risky_steps(self) -> None:
        capability = load_capability(ARTIFACT).model_copy(update={"recorded_by": "alice"})
        with pytest.raises(ValueError, match="maker-checker"):
            sign_risk_review(capability, "alice")

    def test_distinct_reviewer_signs_and_validates(self) -> None:
        capability = load_capability(ARTIFACT).model_copy(update={"recorded_by": "alice"})
        signed = sign_risk_review(capability, "bob")
        assert signed.risk_review.reviewed_by == "bob"
        assert risk_review_valid(signed)

    def test_legacy_artifact_without_maker_still_signs(self) -> None:
        capability = load_capability(ARTIFACT)
        assert capability.recorded_by is None
        signed = sign_risk_review(capability, "alice")
        assert risk_review_valid(signed)

    def test_rewriting_authorship_after_signing_voids_the_signature(self) -> None:
        capability = load_capability(ARTIFACT).model_copy(update={"recorded_by": "alice"})
        signed = sign_risk_review(capability, "bob")
        laundered = signed.model_copy(update={"recorded_by": "someone_else"})
        assert not risk_review_valid(laundered)  # authorship is inside the signed content

    def test_rewriting_the_checker_after_signing_voids_the_signature(self) -> None:
        # The checker's identity is bound into the hash: keeping the stored
        # hash but swapping who "approved" must invalidate the review.
        capability = load_capability(ARTIFACT).model_copy(update={"recorded_by": "alice"})
        signed = sign_risk_review(capability, "bob")
        forged = signed.model_copy(
            update={
                "risk_review": RiskReview(
                    reviewed_by="mallory", artifact_hash=signed.risk_review.artifact_hash
                )
            }
        )
        assert not risk_review_valid(forged)

    def test_identity_variants_do_not_bypass_four_eyes(self) -> None:
        capability = load_capability(ARTIFACT).model_copy(update={"recorded_by": "Alice"})
        with pytest.raises(ValueError, match="maker-checker"):
            sign_risk_review(capability, "alice")  # case variant
        with pytest.raises(ValueError, match="maker-checker"):
            sign_risk_review(capability, " Alice ")  # whitespace variant


def test_every_committed_signed_artifact_validates() -> None:
    """Schema evolution changes the canonical serialization and voids prior
    signatures — that is the hash binding working. This test makes a missed
    re-review impossible to ship: any committed artifact that claims a signer
    must actually validate."""
    paths = sorted((REPO / "capabilities").rglob("*.json"))
    checked = 0
    for path in paths:
        if "request" in path.name:
            continue
        capability = load_capability(path)
        if capability.risk_review.reviewed_by is not None:
            assert risk_review_valid(capability), f"stale signature in {path.name}"
            checked += 1
    assert checked >= 1  # the suite must actually be exercising signed artifacts
