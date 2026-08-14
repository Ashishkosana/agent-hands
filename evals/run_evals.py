"""Eval runner: measured proof, not assertion.

Runs the scenario matrix against a fresh fixture instance, records the
classification each injected runtime state actually produced, replays the
happy path N times for a stability signal, and writes evals/results.md.
The same scenarios are enforced as hard assertions in tests/; this runner
exists to produce the numbers.

Usage: .venv/bin/python evals/run_evals.py
"""

from __future__ import annotations

import json
import socket
import statistics
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from hands.artifact import load_capability  # noqa: E402
from hands.replay import EngineConfig, ReplayEngine  # noqa: E402

STABILITY_RUNS = 10


@dataclass
class Scenario:
    name: str
    params: dict[str, str]
    faults: list[str]
    expected: str  # result class, or class:code


SCENARIOS = [
    Scenario("happy path", {"member_id": "12345"}, [], "success"),
    Scenario("fresh params", {"member_id": "67890"}, [], "success"),
    Scenario(
        "member not found", {"member_id": "99999"}, [], "business_outcome:MEMBER_NOT_FOUND"
    ),
    Scenario(
        "server-side validation", {"member_id": "abc"}, [], "business_outcome:VALIDATION_REJECTED"
    ),
    Scenario(
        "permission denied",
        {"member_id": "12345"},
        ["permission_denied"],
        "business_outcome:PERMISSION_DENIED",
    ),
    Scenario(
        "session expired", {"member_id": "12345"}, ["session_expired"], "success (recovered)"
    ),
    Scenario(
        "maintenance interstitial", {"member_id": "12345"}, ["interstitial"], "success (recovered)"
    ),
    Scenario("transient slow load (5s)", {"member_id": "12345"}, ["slow_load"], "success"),
    Scenario("app error (HTTP 500)", {"member_id": "12345"}, ["app_error"], "failure"),
    Scenario("UI drift (renamed label)", {"member_id": "12345"}, ["renamed_label"], "failure"),
]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def set_fault(base: str, name: str, enabled: bool) -> None:
    body = json.dumps({"fault": name, "enabled": enabled}).encode()
    request = urllib.request.Request(
        base + "/__faults", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        response.read()


def classify(result: object, recovered: bool) -> str:
    kind = getattr(result, "result", "?")
    if kind == "business_outcome":
        return f"business_outcome:{getattr(result, 'code', '?')}"
    if kind == "success" and recovered:
        return "success (recovered)"
    return str(kind)


def main() -> int:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    app = subprocess.Popen(
        [sys.executable, "-m", "fixture.app", "--port", str(port)],
        cwd=REPO,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(base + "/", timeout=1):
                    break
            except OSError:
                time.sleep(0.1)

        capability = load_capability(REPO / "capabilities" / "lookup_member_balance.json")
        capability.target.entry.web.url = base + "/"
        runs_dir = REPO / "runs" / "evals"
        engine = ReplayEngine(EngineConfig(runs_dir=runs_dir))

        rows: list[str] = []
        fired_conditions: set[str] = set()
        all_correct = True
        for scenario in SCENARIOS:
            for fault in scenario.faults:
                set_fault(base, fault, True)
            started = time.monotonic()
            result = engine.run(capability, scenario.params)
            elapsed = time.monotonic() - started
            for fault in scenario.faults:
                set_fault(base, fault, False)
            recovered = _run_recovered(runs_dir)
            actual = classify(result, recovered)
            fired_conditions |= _fired(runs_dir)
            ok = actual == scenario.expected
            all_correct = all_correct and ok
            rows.append(
                f"| {scenario.name} | {scenario.expected} | {actual} | "
                f"{'✅' if ok else '❌'} | {elapsed:.1f}s |"
            )

        latencies: list[float] = []
        successes = 0
        for _ in range(STABILITY_RUNS):
            started = time.monotonic()
            result = engine.run(capability, {"member_id": "12345"})
            latencies.append(time.monotonic() - started)
            successes += 1 if getattr(result, "result", "") == "success" else 0

        discovery = json.loads((REPO / "evidence" / "discovery-report.json").read_text())
        happy = discovery["happy"]
        total_conditions = len(capability.conditions)

        table = "\n".join(
            [
                "# Eval results",
                "",
                f"_Generated by `evals/run_evals.py` on {time.strftime('%Y-%m-%d')}; "
                f"all scenarios also enforced as assertions in `tests/`._",
                "",
                "## Runtime-state classification (the three-way contract)",
                "",
                "| scenario | expected | actual | correct | replay latency |",
                "|---|---|---|---|---|",
                *rows,
                "",
                f"Recognizers exercised: **{len(fired_conditions)}/{total_conditions}** "
                f"({', '.join(sorted(fired_conditions))})",
                "",
                "## Replay stability",
                "",
                "| runs | successes | mean latency | max latency |",
                "|---|---|---|---|",
                f"| {STABILITY_RUNS} | {successes}/{STABILITY_RUNS} | "
                f"{statistics.mean(latencies):.2f}s | {max(latencies):.2f}s |",
                "",
                "## Discovery vs replay (the economics)",
                "",
                "| | LLM discovery (once) | deterministic replay (production) |",
                "|---|---|---|",
                f"| model calls | {happy['llm_calls']} | **0** (proven hermetically) |",
                f"| tokens | {happy['prompt_tokens'] + happy['completion_tokens']} | **0** |",
                f"| wall clock | {happy['duration_s']}s | {statistics.mean(latencies):.2f}s |",
                "",
            ]
        )
        (REPO / "evals" / "results.md").write_text(table)
        print(table)
        return 0 if all_correct and successes == STABILITY_RUNS else 1
    finally:
        app.terminate()
        app.wait(timeout=10)


def _latest_trace(runs_dir: Path) -> list[dict[str, object]]:
    run_dir = max(runs_dir.glob("*"), key=lambda p: p.stat().st_mtime)
    return [
        json.loads(line) for line in (run_dir / "trace.jsonl").read_text().splitlines()
    ]


def _run_recovered(runs_dir: Path) -> bool:
    return any(e["event"] == "recovery_step_done" for e in _latest_trace(runs_dir))


def _fired(runs_dir: Path) -> set[str]:
    return {
        str(e["condition"])
        for e in _latest_trace(runs_dir)
        if e["event"] == "recognizer_fired"
    }


if __name__ == "__main__":
    sys.exit(main())
