"""The zero-LLM replay proof, hermetic version.

Replay runs in a fresh interpreter with (a) every API-key-shaped environment
variable stripped and (b) non-loopback network egress blocked at the socket
level. If any model were consulted anywhere in the replay path, this test
could not pass. An in-process import check would be order-dependent and
name-brittle; a subprocess is neither.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from hands.artifact import Capability, dump_capability
from tests.conftest import REPO_ROOT

_SECRET_SHAPED = re.compile(r"KEY|TOKEN|SECRET|ANTHROPIC|OPENAI|CREDENTIAL", re.IGNORECASE)


def test_replay_is_hermetic(capability: Capability, tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.json"
    artifact.write_text(dump_capability(capability))

    env = {k: v for k, v in os.environ.items() if not _SECRET_SHAPED.search(k)}
    proc = subprocess.run(
        [
            sys.executable,
            "tests/hermetic_runner.py",
            "replay",
            str(artifact),
            "--param",
            "member_id=12345",
            "--runs-dir",
            str(tmp_path / "runs"),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}\nstdout: {proc.stdout}"
    result = json.loads(proc.stdout)
    assert result["result"] == "success"
    assert result["outputs"]["savings_balance"] == "1234.50"

    # Zero model events in the trace: every event kind is a known engine event.
    engine_events = {
        "run_started", "requires_met", "requires_unmet", "step_started",
        "recognizers_suppressed_stale", "recognizer_suppression_lifted",
        "acted", "act_attempt_failed", "action_effect_detected",
        "postconditions_met", "recognizer_fired", "checkpoint_verified",
        "output_extracted", "run_finished",
    }
    (run_dir,) = (tmp_path / "runs").iterdir()
    events = {
        json.loads(line)["event"]
        for line in (run_dir / "trace.jsonl").read_text().splitlines()
    }
    assert events <= engine_events, f"unexpected events: {events - engine_events}"
