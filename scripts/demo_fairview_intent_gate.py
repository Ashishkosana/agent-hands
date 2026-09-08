"""Fairview Transfer Intent Gate demo — writes a hash-chained run under runs/.

Starts the fixture, proves unattended fail-closed, then runs an attended
Approve against the real Post Transfer control so the trust dashboard can
show intent_approved + verify_chain.

Usage (from repo root):

    python scripts/demo_fairview_intent_gate.py
    python -m dashboard.app --port 8200   # then open http://127.0.0.1:8200
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from hands.artifact import load_capability, sign_risk_review  # noqa: E402
from hands.replay import (  # noqa: E402
    EngineConfig,
    EscalationSettings,
    PolicySettings,
    ReplayEngine,
)
from hands.results import PolicyViolation, Success  # noqa: E402
from hands.trace import verify_chain  # noqa: E402
from tests.test_escalation import Operator  # noqa: E402

ARTIFACT = REPO / "capabilities" / "generated" / "fairview_funds_transfer.json"
PARAMS = {
    "operator_id": "teller1",
    "password": "password",
    "member_id": "12345",
    "from_share": "S1 - Savings",
    "to_share": "S2 - Checking",
    "amount": "1.00",
    "memo": "demo intent gate",
}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_http(url: str, seconds: float = 15) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"fixture did not start at {url}")


def main() -> int:
    dest = REPO / "runs"
    dest.mkdir(exist_ok=True)

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, "-m", "fixture.app", "--port", str(port)],
        cwd=REPO,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    scratch = Path(tempfile.mkdtemp(prefix="fairview-intent-"))
    try:
        _wait_http(base + "/")
        cap = load_capability(ARTIFACT)
        cap.target.entry.web.url = base + "/"
        signed = sign_risk_review(cap, "demo-reviewer")

        unattended = ReplayEngine(
            EngineConfig(
                runs_dir=scratch,
                policy=PolicySettings(require_intent_approval=True),
            )
        ).run(signed, PARAMS)
        if not isinstance(unattended, PolicyViolation):
            print(f"expected policy_violation, got {unattended}", file=sys.stderr)
            return 1
        print(f"unattended: {unattended.rule} — {unattended.detail}")

        operator = Operator(
            scratch, [{"do": "await", "state": "paused"}, {"do": "approve"}]
        )
        operator.start()
        attended = ReplayEngine(
            EngineConfig(
                runs_dir=scratch,
                escalation=EscalationSettings(ttl_s=60.0, console_port=0),
                policy=PolicySettings(require_intent_approval=True),
            )
        )
        result = attended.run(cap, PARAMS)
        operator.join()
        if not isinstance(result, Success):
            print(f"attended approve failed: {result}", file=sys.stderr)
            return 1
        run_dir = attended.last_run_dir
        assert run_dir is not None
        intact = verify_chain(run_dir)
        events = [
            json.loads(line) for line in (run_dir / "trace.jsonl").read_text().splitlines()
        ]
        kinds = [e["event"] for e in events]
        published = dest / run_dir.name
        if published.exists():
            shutil.rmtree(published)
        shutil.copytree(run_dir, published)
        print(
            f"attended: success confirmation={result.outputs.get('confirmation_number')} "
            f"run={run_dir.name} chain={intact} intent_approved={'intent_approved' in kinds}"
        )
        print("dashboard: python -m dashboard.app --port 8200")
        print(f"explain:   hands explain {run_dir.name}")
        return 0 if intact and "intent_approved" in kinds else 1
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
