"""Verifier-twin evaluation: measured classification, not a demo.

Two modes, reported separately and never mixed:

  local  (default)  Fault-injected runs against the MERIDIAN-shaped local
                    fixture (fixture/meridian.py), N per scenario, each run
                    from a reset state. Ground truth comes from the fixture's
                    /__state, so every verdict is checked against what
                    actually happened — not against what the UI showed.

  --live            ONE verified run against the real MERIDIAN console with
                    the original signed artifacts. Place Hold is consequential
                    there (the share stays held), so this is run deliberately
                    and rarely. Ground truth is whatever the independent
                    verifier read; there is no back door into the live core.

Both write raw rows to evals/twin_results.json and regenerate
evals/twin_results.md. Representative evidence bundles are copied into
evidence/twin/ (runs/ is transient and gitignored).

Usage:
  .venv/bin/python evals/run_twin_evals.py                  # local matrix, N=10
  .venv/bin/python evals/run_twin_evals.py --runs 4
  HANDS_PARAM_PASSWORD=... .venv/bin/python evals/run_twin_evals.py --live \
      --live-scenario COMMIT_WITH_LOST_ACK --member 101555 --share 101555-CERT \
      --share-option Certificate
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from hands.artifact import dump_capability, load_capability  # noqa: E402
from hands.chaos import ChaosMode, FaultInjector  # noqa: E402
from hands.reconcile import ExpectedEffect, Verdict  # noqa: E402
from hands.twin import TwinConfig, TwinRun, retarget, run_twin, verdict_of  # noqa: E402

GEN = REPO / "capabilities" / "generated"
RESULTS_JSON = REPO / "evals" / "twin_results.json"
RESULTS_MD = REPO / "evals" / "twin_results.md"
EVIDENCE = REPO / "evidence" / "twin"
LIVE_ENTRY = "https://web-sample.interface-hiring.com/"


@dataclass(frozen=True)
class Scenario:
    name: str
    executor_chaos: ChaosMode | None
    verifier_chaos: ChaosMode | None
    expected: str
    # (member, share_id, share option substring) — rotated per run for variety
    targets: tuple[tuple[str, str, str], ...]


OPEN_TARGETS = (
    ("100234", "100234-S0070", "Share Draft (Checking)"),
    ("100987", "100987-S0001", "Regular Shares"),
    ("101555", "101555-CERT", "Certificate"),
    ("102777", "102777-MMKT", "Money Market"),
)

SCENARIOS = [
    Scenario("baseline (no fault)", None, None, "VERIFIED_COMMITTED", OPEN_TARGETS),
    Scenario("A. COMMIT_WITH_LOST_ACK", ChaosMode.COMMIT_WITH_LOST_ACK, None,
             "VERIFIED_COMMITTED", OPEN_TARGETS),
    Scenario("B. NO_COMMIT_WITH_LOST_ACK", ChaosMode.NO_COMMIT_WITH_LOST_ACK, None,
             "VERIFIED_NOT_COMMITTED", OPEN_TARGETS),
    Scenario("C. FALSE_SUCCESS", ChaosMode.FALSE_SUCCESS, None, "EFFECT_MISMATCH", OPEN_TARGETS),
    Scenario("D. VERIFIER_FAILURE", None, ChaosMode.VERIFIER_FAILURE, "UNVERIFIABLE",
             OPEN_TARGETS),
    Scenario("E. pre-image already HOLD", None, None, "REFUSED",
             (("103001", "103001-S0001", "Regular Shares"),)),
]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_up(base: str) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base + "/signon", timeout=1):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("fixture did not start")


def reset(base: str) -> None:
    urllib.request.urlopen(urllib.request.Request(base + "/__reset", method="POST")).read()


def truth(base: str, share_id: str) -> str:
    with urllib.request.urlopen(base + "/__state", timeout=5) as response:
        data = json.loads(response.read())
    return str(data["shares"][share_id])


def params_for(member: str, share_option: str, operator: str, password: str) -> dict[str, str]:
    return {"operator_id": operator, "password": password, "member_number": member,
            "share": share_option, "reason": "FRAUD", "notes": "verifier twin evaluation"}


def row_from(run: TwinRun, scenario: str, expected: str, ground_truth: str | None,
             elapsed: float) -> dict[str, object]:
    actual = verdict_of(run)
    rec = run.reconciliation
    executor = run.executor
    exec_result = executor.result if executor else {}
    row: dict[str, object] = {
        "scenario": scenario,
        "expected": expected,
        "actual": actual,
        "correct": actual == expected,
        "executed": run.executed,
        "executor_result": executor.result_kind if executor else None,
        "executor_dispatched": exec_result.get("dispatched") if executor else None,
        "verifier_pre": rec.target_pre if rec else (
            run.pre.observation.status_of(run.expected.share_id)
            if run.pre and run.pre.observation else None),
        "verifier_post": rec.target_post if rec else None,
        "verifier_post_ok": run.post.ok if run.post else None,
        "reconciliation": rec.verdict.value if rec else None,
        "reason": rec.reason if rec else run.aborted,
        "retry_eligible": rec.retry_eligible if rec else None,
        "ground_truth_after": ground_truth,
        "duration_s": round(elapsed, 2),
        "twin_id": run.twin_id,
        "member": run.expected.member_number,
        "share": run.expected.share_id,
        "fault_mode": run.fault_mode,
    }
    # False-confident detection against ground truth (local only).
    if ground_truth is not None and rec is not None:
        to = run.expected.to_status
        row["false_verified"] = rec.verdict is Verdict.VERIFIED_COMMITTED and ground_truth != to
        row["false_not_committed"] = (
            rec.verdict is Verdict.VERIFIED_NOT_COMMITTED and ground_truth == to
        )
    else:
        row["false_verified"] = False
        row["false_not_committed"] = False
    return row


def copy_bundle(run: TwinRun, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for label, run_dir in run.manifest.run_dirs.items():
        if run_dir and Path(run_dir).exists():
            shutil.copytree(run_dir, dest / label)


def run_local(runs_per_scenario: int) -> dict[str, object]:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    app = subprocess.Popen(
        [sys.executable, "-m", "fixture.meridian", "--port", str(port)],
        cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    rows: list[dict[str, object]] = []
    representative: dict[str, TwinRun] = {}
    try:
        wait_up(base)
        work = Path(tempfile.mkdtemp(prefix="twin-eval-"))
        artifacts: dict[str, Path] = {}
        for name in ("meridian_place_hold", "meridian_member_record"):
            cap = retarget(load_capability(GEN / f"{name}.json"), base, "eval-harness")
            artifacts[name] = work / f"{name}.json"
            artifacts[name].write_text(dump_capability(cap))
        runs_dir = REPO / "runs" / "twin-evals"
        for scenario in SCENARIOS:
            n = runs_per_scenario if scenario.expected != "REFUSED" else 2
            for i in range(n):
                member, share_id, option = scenario.targets[i % len(scenario.targets)]
                reset(base)
                injector = (
                    FaultInjector(scenario.executor_chaos) if scenario.executor_chaos else None
                )
                cfg = TwinConfig(
                    executor_artifact=artifacts["meridian_place_hold"],
                    verifier_artifact=artifacts["meridian_member_record"],
                    executor_params=params_for(member, option, "super1", "password"),
                    verifier_params={"operator_id": "teller1", "password": "password",
                                     "member_number": member},
                    expected=ExpectedEffect(member_number=member, share_id=share_id),
                    runs_dir=runs_dir,
                    executor_chaos=injector,
                    verifier_chaos=scenario.verifier_chaos,
                    step_budget_s=4.0,
                )
                started = time.monotonic()
                run = run_twin(cfg)
                elapsed = time.monotonic() - started
                row = row_from(run, scenario.name, scenario.expected, truth(base, share_id),
                               elapsed)
                rows.append(row)
                mark = "ok " if row["correct"] else "BAD"
                print(f"[{mark}] {scenario.name:28s} run {i + 1:2d}/{n}: {row['actual']:24s} "
                      f"exec={row['executor_result']!s:12s} truth={row['ground_truth_after']} "
                      f"{elapsed:5.1f}s", flush=True)
                if scenario.name not in representative:
                    representative[scenario.name] = run
        for name, run in representative.items():
            slug = name.split(". ", 1)[-1].split(" ")[0].lower().replace("(", "").replace(")", "")
            copy_bundle(run, EVIDENCE / "local" / slug)
    finally:
        app.terminate()
        app.wait(timeout=10)
    return {
        "generated": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "runs_per_scenario": runs_per_scenario,
        "step_budget_s": 4.0,
        "rows": rows,
    }


def run_live(scenario_name: str, member: str, share_id: str, share_option: str,
             executor_operator: str, verifier_operator: str) -> dict[str, object]:
    password = os.environ.get("HANDS_PARAM_PASSWORD")
    if not password:
        raise SystemExit("--live requires HANDS_PARAM_PASSWORD in the environment")
    scenario = next(s for s in SCENARIOS if s.name.endswith(scenario_name))
    injector = FaultInjector(scenario.executor_chaos) if scenario.executor_chaos else None
    cfg = TwinConfig(
        executor_artifact=GEN / "meridian_place_hold.json",
        verifier_artifact=GEN / "meridian_member_record.json",
        executor_params=params_for(member, share_option, executor_operator, password),
        verifier_params={"operator_id": verifier_operator, "password": password,
                         "member_number": member},
        expected=ExpectedEffect(member_number=member, share_id=share_id),
        runs_dir=REPO / "runs" / "twin-live",
        executor_chaos=injector,
        verifier_chaos=scenario.verifier_chaos,
    )
    started = time.monotonic()
    run = run_twin(cfg)
    elapsed = time.monotonic() - started
    row = row_from(run, scenario.name, scenario.expected, None, elapsed)
    row["target"] = LIVE_ENTRY
    row["chaos_fired"] = run.manifest.chaos_fired
    row["generated"] = datetime.now(tz=UTC).isoformat(timespec="seconds")
    print(json.dumps(row, indent=2, default=str))
    copy_bundle(run, EVIDENCE / "live" / f"{scenario_name.lower()}-{run.twin_id}")
    return row


def load_results() -> dict[str, object]:
    if RESULTS_JSON.exists():
        data: dict[str, object] = json.loads(RESULTS_JSON.read_text())
        return data
    return {"local": None, "live": []}


def write_report(results: dict[str, object]) -> None:
    lines: list[str] = ["# Verifier-twin evaluation results", ""]
    lines.append(
        "_Generated by `evals/run_twin_evals.py`. Local fault-injection numbers and live "
        "MERIDIAN runs are reported in separate sections and must not be added together._"
    )
    lines.append("")
    local = results.get("local")
    if isinstance(local, dict):
        rows = local["rows"]
        assert isinstance(rows, list)
        total = len(rows)
        correct = sum(1 for r in rows if r["correct"])
        judged = [r for r in rows if r["reconciliation"] is not None]
        unverifiable = [r for r in judged if r["reconciliation"] == "UNVERIFIABLE"]
        spurious_unverifiable = [r for r in unverifiable if r["expected"] != "UNVERIFIABLE"]
        false_verified = [r for r in rows if r["false_verified"]]
        false_not = [r for r in rows if r["false_not_committed"]]
        refused = [r for r in rows if r["actual"] == "REFUSED"]
        lines += [
            "## Local fault-injection matrix (MERIDIAN-shaped fixture)",
            "",
            f"Generated {local['generated']}; {local['runs_per_scenario']} runs per fault "
            f"scenario (2 for the refusal scenario); executor/verifier step budget "
            f"{local['step_budget_s']}s; each run from a reset fixture; ground truth from the "
            "fixture's `/__state`, never from the UI.",
            "",
            "| metric | value |",
            "|---|---|",
            f"| total verified runs | **{total}** |",
            f"| correct classifications (actual == expected) | **{correct}/{total}** |",
            f"| false VERIFIED_COMMITTED (verdict committed, truth not) | "
            f"**{len(false_verified)}** |",
            f"| false VERIFIED_NOT_COMMITTED (verdict not committed, truth committed) | "
            f"**{len(false_not)}** |",
            f"| UNVERIFIABLE (count / rate over judged runs) | **{len(unverifiable)} / "
            f"{len(judged)}** ({100 * len(unverifiable) / max(1, len(judged)):.1f}%) |",
            f"| UNVERIFIABLE where the verifier was NOT deliberately broken (spurious) | "
            f"**{len(spurious_unverifiable)}** |",
            f"| execution refused on pre-image (fail closed, nothing posted) | {len(refused)} |",
            "",
            "### Per-scenario",
            "",
            "| scenario | expected | runs | correct | executor result(s) | verdict(s) | "
            "mean duration |",
            "|---|---|---|---|---|---|---|",
        ]
        for scenario in SCENARIOS:
            srows = [r for r in rows if r["scenario"] == scenario.name]
            if not srows:
                continue
            execs = sorted({str(r["executor_result"]) for r in srows})
            verdicts = sorted({str(r["actual"]) for r in srows})
            ok = sum(1 for r in srows if r["correct"])
            mean = statistics.mean(float(r["duration_s"]) for r in srows)
            lines.append(
                f"| {scenario.name} | {scenario.expected} | {len(srows)} | {ok}/{len(srows)} | "
                f"{', '.join(execs)} | {', '.join(verdicts)} | {mean:.1f}s |"
            )
        lines += [
            "",
            "### Every run",
            "",
            "| # | scenario | expected | actual | ok | executor | dispatched | pre | post | "
            "truth after | s | evidence |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for i, r in enumerate(rows, 1):
            lines.append(
                f"| {i} | {r['scenario']} | {r['expected']} | {r['actual']} | "
                f"{'✅' if r['correct'] else '❌'} | {r['executor_result']} | "
                f"{r['executor_dispatched']} | {r['verifier_pre']} | {r['verifier_post']} | "
                f"{r['ground_truth_after']} | {r['duration_s']} | `{r['twin_id']}` |"
            )
        lines += [
            "",
            "Representative evidence bundles (twin trace, executor run, both verifier runs) are "
            "in `evidence/twin/local/<scenario>/`. Full run output is under `runs/twin-evals/` "
            "(transient).",
            "",
        ]
    live = results.get("live")
    lines += ["## Live MERIDIAN validation", ""]
    if isinstance(live, list) and live:
        lines += [
            "Each row is ONE verified run against the real console with the original signed "
            "artifacts; the executor signed on as a supervisor, the verifier as a teller (a "
            "different login — the same credential class). No ground-truth back door exists on "
            "the live core: 'truth' here is what the independent verifier read.",
            "",
            "| when | scenario | expected | actual | executor | dispatched | pre | post | "
            "chaos record | s | evidence |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for r in live:
            fired = r.get("chaos_fired") or []
            chaos = "; ".join(
                f"server_status={f.get('server_status')} withheld={f.get('response_withheld')}"
                if "server_status" in f else str({k: v for k, v in f.items() if k != "url"})
                for f in fired
            ) or "—"
            lines.append(
                f"| {r['generated']} | {r['scenario']} | {r['expected']} | {r['actual']} | "
                f"{r['executor_result']} | {r['executor_dispatched']} | {r['verifier_pre']} | "
                f"{r['verifier_post']} | {chaos} | {r['duration_s']} | `{r['twin_id']}` |"
            )
        lines += ["", "Evidence bundles: `evidence/twin/live/`.", ""]
    else:
        lines += ["_No live run recorded yet._", ""]
    RESULTS_MD.write_text("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=10, help="runs per local scenario")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--live-scenario", default="COMMIT_WITH_LOST_ACK",
                        choices=[s.name.split(". ", 1)[-1] for s in SCENARIOS])
    parser.add_argument("--member", default="101555")
    parser.add_argument("--share", default="101555-CERT")
    parser.add_argument("--share-option", default="Certificate")
    parser.add_argument("--executor-operator", default="super1")
    parser.add_argument("--verifier-operator", default="teller1")
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()

    results = load_results()
    if args.report_only:
        write_report(results)
        return 0
    if args.live:
        row = run_live(args.live_scenario, args.member, args.share, args.share_option,
                       args.executor_operator, args.verifier_operator)
        live = results.get("live")
        if not isinstance(live, list):
            live = []
        live.append(row)
        results["live"] = live
        RESULTS_JSON.write_text(json.dumps(results, indent=2, default=str) + "\n")
        write_report(results)
        return 0 if row["correct"] else 1
    local = run_local(args.runs)
    results["local"] = local
    RESULTS_JSON.write_text(json.dumps(results, indent=2, default=str) + "\n")
    write_report(results)
    rows = local["rows"]
    assert isinstance(rows, list)
    bad = [r for r in rows if not r["correct"] or r["false_verified"] or r["false_not_committed"]]
    print(f"\n{len(rows) - len(bad)}/{len(rows)} correct; false-confident: "
          f"{sum(1 for r in rows if r['false_verified'] or r['false_not_committed'])}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
