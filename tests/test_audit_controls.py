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
    load_capability,
    risk_review_valid,
    sign_risk_review,
)
from hands.trace import Trace, is_chained, verify_chain

ARTIFACT = Path(__file__).resolve().parent.parent / "capabilities" / "lookup_member_balance.json"


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
