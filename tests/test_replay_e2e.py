"""End-to-end replay against the live fixture app — the production path.

No LLM exists anywhere in these tests; that is the point of the slice.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from hands.artifact import Capability, LabelRung, TextRung
from hands.replay import EngineConfig, ReplayEngine
from hands.results import BusinessOutcome, Failure, Success


def engine(tmp_path: Path) -> ReplayEngine:
    return ReplayEngine(EngineConfig(runs_dir=tmp_path / "runs"))


def test_happy_path_returns_declared_outputs(capability: Capability, tmp_path: Path) -> None:
    result = engine(tmp_path).run(capability, {"member_id": "12345"})
    assert isinstance(result, Success)
    assert result.outputs == {"savings_balance": Decimal("1234.50")}


def test_fresh_params_reuse_the_same_artifact(capability: Capability, tmp_path: Path) -> None:
    result = engine(tmp_path).run(capability, {"member_id": "67890"})
    assert isinstance(result, Success)
    assert result.outputs == {"savings_balance": Decimal("52.00")}


def test_member_not_found_is_an_answer_not_a_crash(
    capability: Capability, tmp_path: Path
) -> None:
    result = engine(tmp_path).run(capability, {"member_id": "99999"})
    assert isinstance(result, BusinessOutcome)
    assert result.code == "MEMBER_NOT_FOUND"
    assert "No member matches ID 99999" in result.evidence.matched_text
    assert result.evidence.region == "results"


def test_missing_element_fails_loudly_with_evidence(
    capability: Capability, tmp_path: Path
) -> None:
    # Simulate the app having renamed its field: no ladder rung can resolve.
    capability.steps[0].target.ladder = [  # type: ignore[union-attr]
        LabelRung(text="Member Number"),
        TextRung(text="Member Number"),
    ]
    result = engine(tmp_path).run(capability, {"member_id": "12345"})
    assert isinstance(result, Failure)
    assert result.report.step_id == "s1"
    assert "no rung matched" in result.report.observed
    assert result.report.screenshot_path is not None
    assert Path(result.report.screenshot_path).exists()
    assert result.report.snapshot_path is not None


def test_wrong_params_rejected_before_any_browser_work(
    capability: Capability, tmp_path: Path
) -> None:
    import pytest

    with pytest.raises(ValueError, match="missing parameters"):
        engine(tmp_path).run(capability, {})
    # "abc" passes the (deliberately loose) client-side pattern so the SERVER
    # can be the validation authority — see test_taxonomy. This one can't:
    with pytest.raises(ValueError, match="does not match pattern"):
        engine(tmp_path).run(capability, {"member_id": "far-too-long-for-the-pattern"})


def test_trace_records_rung_telemetry_and_masks_sensitive_outputs(
    capability: Capability, tmp_path: Path
) -> None:
    result = engine(tmp_path).run(capability, {"member_id": "12345"})
    assert isinstance(result, Success)

    run_dirs = list((tmp_path / "runs").iterdir())
    assert len(run_dirs) == 1
    events = [
        json.loads(line)
        for line in (run_dirs[0] / "trace.jsonl").read_text().splitlines()
    ]
    kinds = [e["event"] for e in events]
    assert kinds[0] == "run_started" and kinds[-1] == "run_finished"
    assert len(events[0]["artifact_sha256"]) == 64  # run attributable to contract

    # Rung telemetry: the unlabeled legacy input resolves on the proximity
    # rung (index 1), not the label rung — recorded, not asserted away.
    acted = [e for e in events if e["event"] == "acted"]
    assert acted[0]["rung_index"] == 1
    assert acted[1]["rung_index"] == 0

    # The savings balance is declared sensitive: real value in the returned
    # result, masked in the persisted trace.
    finished = events[-1]
    assert finished["result"]["outputs"]["savings_balance"] == "«masked»"
    raw = (run_dirs[0] / "trace.jsonl").read_text()
    assert "1234.50" not in raw
