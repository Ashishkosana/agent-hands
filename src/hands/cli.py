"""Command-line interface.

Exit codes: 0 = the system worked (SUCCESS or a BUSINESS_OUTCOME — an answer
is not a malfunction); 1 = FAILURE, UNRESOLVED, PRECONDITION_FAILED or
POLICY_VIOLATION; 2 = usage error. UNRESOLVED shares exit 1 deliberately: a
shell script must not treat it as "worked", and the JSON on stdout carries the
distinction for anything that can read it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import TypeAdapter

from hands.artifact import load_capability
from hands.replay import EngineConfig, EscalationSettings, ReplayEngine
from hands.results import BusinessOutcome, ReplayResult, Success

_RESULT_ADAPTER: TypeAdapter[ReplayResult] = TypeAdapter(ReplayResult)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hands")
    sub = parser.add_subparsers(dest="command", required=True)

    replay = sub.add_parser("replay", help="deterministically replay a capability artifact")
    replay.add_argument("artifact", type=Path)
    replay.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="invocation parameter (repeatable)",
    )
    replay.add_argument("--headed", action="store_true", help="show the browser")
    replay.add_argument("--runs-dir", type=Path, default=Path("runs"))
    replay.add_argument(
        "--attended",
        action="store_true",
        help="failures raise an intervention for a human operator instead of returning",
    )
    replay.add_argument("--console-port", type=int, default=8321)
    replay.add_argument("--ttl", type=float, default=300.0, help="intervention TTL seconds")

    review = sub.add_parser(
        "review", help="sign a capability's risk labels after reviewing the artifact"
    )
    review.add_argument("artifact", type=Path)
    review.add_argument("--operator", required=True, help="reviewer identity, recorded")

    discover_cmd = sub.add_parser(
        "discover", help="LLM-driven discovery: learn a flow and record it as a capability"
    )
    discover_cmd.add_argument("request", type=Path, help="a discovery request JSON file")
    discover_cmd.add_argument("--headed", action="store_true", help="show the browser")
    discover_cmd.add_argument("--runs-dir", type=Path, default=Path("runs"))
    discover_cmd.add_argument("--model", default=None, help="override the configured model")

    explain_cmd = sub.add_parser(
        "explain", help="print an audit receipt reconstructing a recorded run"
    )
    explain_cmd.add_argument("run_id", help="a run directory name under --runs-dir")
    explain_cmd.add_argument("--runs-dir", type=Path, default=Path("runs"))
    explain_cmd.add_argument("--gen-dir", type=Path, default=Path("capabilities/generated"))

    args = parser.parse_args(argv)

    if args.command == "discover":
        return _discover(parser, args)
    if args.command == "review":
        return _review(parser, args)
    if args.command == "explain":
        return _explain(parser, args)

    params: dict[str, str] = {}
    for item in args.param:
        name, sep, value = item.partition("=")
        if not sep or not name:
            parser.error(f"--param expects NAME=VALUE, got {item!r}")
        params[name] = value

    try:
        capability = load_capability(args.artifact)
    except (OSError, ValueError) as exc:
        parser.error(f"cannot load artifact: {exc}")

    escalation = None
    if args.attended:
        escalation = EscalationSettings(ttl_s=args.ttl, console_port=args.console_port)
        print(
            f"operator console: http://127.0.0.1:{args.console_port}/", file=sys.stderr
        )
    engine = ReplayEngine(
        EngineConfig(headed=args.headed, runs_dir=args.runs_dir, escalation=escalation)
    )
    try:
        result = engine.run(capability, params)
    except ValueError as exc:  # parameter validation: a caller bug, not a run result
        parser.error(str(exc))

    print(_RESULT_ADAPTER.dump_json(result, indent=2).decode())
    return 0 if isinstance(result, (Success, BusinessOutcome)) else 1


def _review(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Sign an artifact's risk labels. The reviewer is asserting they read
    the diffable step list; the signature binds to the artifact hash, so any
    later change invalidates it."""
    from hands.artifact import dump_capability, sign_risk_review

    try:
        capability = load_capability(args.artifact)
    except (OSError, ValueError) as exc:
        parser.error(f"cannot load artifact: {exc}")
    risky = [s.id for s in capability.steps if s.risk == "risky"]
    try:
        signed = sign_risk_review(capability, args.operator)
    except ValueError as exc:  # maker-checker refusal — a policy answer, not a crash
        parser.error(str(exc))
    args.artifact.write_text(dump_capability(signed))
    print(
        f"signed by {args.operator!r}: {len(risky)} risky step(s) {risky}, "
        f"hash {signed.risk_review.artifact_hash[:16] if signed.risk_review.artifact_hash else ''}…"
    )
    if risky and capability.recorded_by is None:
        print(
            "note: maker unknown (no recorded_by; set HANDS_OPERATOR at discovery) — "
            "the four-eyes check cannot bind on this artifact",
            file=sys.stderr,
        )
    return 0


def _explain(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Reconstruct a recorded run as a single examiner-grade audit receipt:
    which contract ran (+ hash), who signed it, the full decision trace, proof
    no model was in the loop, the typed result, and the evidence. Assembled
    from files replay already writes — model-free."""
    import hashlib

    from hands.artifact import dump_capability, risk_review_valid, same_operator
    from hands.trace import is_chained, verify_chain

    run_dir = args.runs_dir / args.run_id
    trace_file = run_dir / "trace.jsonl"
    if not trace_file.exists():
        parser.error(f"no trace at {trace_file}")

    events: list[dict[str, object]] = []
    for line in trace_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    started = next((e for e in events if e.get("event") == "run_started"), None)
    finished = next((e for e in events if e.get("event") == "run_finished"), None)
    if started is None:
        parser.error("not a replay run (no run_started event)")
        return 2  # pragma: no cover

    name = str(started.get("capability", "?"))
    version = started.get("version", "?")
    run_hash = str(started.get("artifact_sha256", ""))

    # Contract status vs the artifact on disk now, plus maker/checker identities.
    status, signer, maker_checker = "unknown (artifact not found)", "—", "—"
    art = args.gen_dir / f"{name}.json"
    if art.exists():
        try:
            cap = load_capability(art)
            cur_hash = hashlib.sha256(dump_capability(cap).encode()).hexdigest()
            status = (
                "VERIFIED — byte-identical to the artifact on disk"
                if cur_hash == run_hash
                else "DRIFTED — the artifact changed since this run"
            )
            rv = cap.risk_review
            signer = (
                f"signed by {rv.reviewed_by!r} "
                f"({'valid' if risk_review_valid(cap) else 'INVALID'})"
                if rv.reviewed_by
                else "unsigned"
            )
            maker = cap.recorded_by or "(not recorded)"
            checker = rv.reviewed_by or "(unsigned)"
            if same_operator(cap.recorded_by, rv.reviewed_by):
                distinct = "SELF-APPROVED -- four-eyes VIOLATED"
            elif not rv.reviewed_by:
                distinct = "unsigned"
            elif not cap.recorded_by:
                distinct = "maker unknown -- four-eyes not bindable"
            else:
                distinct = "distinct ok (four-eyes)"
            maker_checker = f"recorded by {maker!r} / approved by {checker!r} ({distinct})"
        except (OSError, ValueError):
            pass

    # Tamper evidence: recompute the trace hash chain.
    if is_chained(run_dir):
        chain = (
            "TAMPER-EVIDENT: intact (hash chain verifies)"
            if verify_chain(run_dir)
            else "TAMPER-EVIDENT: BROKEN — the log was modified after the run"
        )
    else:
        chain = "legacy trace (recorded before hash-chaining)"

    model_events = [
        e for e in events
        if any(k in str(e.get("event", "")) for k in ("llm", "model", "planner", "discovery"))
    ]

    bar = "=" * 62
    out = [bar, f" AUDIT RECEIPT  ·  {args.run_id}", bar,
           f" Capability:      {name}  v{version}",
           f" Contract hash:   {run_hash}",
           f" Contract status: {status}",
           f" Risk sign-off:   {signer}",
           f" Maker / Checker: {maker_checker}",
           f" Audit log:       {chain}",
           f" Inputs (masked): {started.get('params', {})}",
           "", " -- Decision trace --"]
    for e in events:
        ev = str(e.get("event", ""))
        ts = str(e.get("ts", ""))[11:23]
        if ev == "step_started":
            out.append(f"  {ts}  STEP {e.get('step')}  {e.get('intent')}  ({e.get('risk')})")
        elif ev == "acted":
            out.append(f"  {ts}       -> acted ({e.get('action')}) via {e.get('rung')}")
        elif ev == "postconditions_met":
            out.append(f"  {ts}       ok verified")
        elif ev == "recognizer_fired":
            out.append(f"  {ts}  [!] recognized {e.get('outcome')} -- {e.get('matched')}")
        elif ev == "checkpoint_verified":
            out.append(f"  {ts}  CHECKPOINT ok -- confirmed the right record")
        elif ev == "output_extracted":
            out.append(f"  {ts}  OUTPUT {e.get('name')} = {e.get('value')}")
        elif ev.startswith(("escalat", "operator_", "human_", "evidence_suppressed")):
            out.append(f"  {ts}  [human] {ev}")
    det = "NO model in the decision loop" if not model_events else "WARNING: model events present"
    out += ["", " -- Determinism --",
            f" Model/LLM events in this run: {len(model_events)}  ->  {det}",
            "", " -- Result --",
            f" {json.dumps((finished or {}).get('result', {}))}",
            "", " -- Evidence --"]
    ev_files = sorted(p.name for p in run_dir.iterdir() if p.name != "trace.jsonl")
    out.append(" " + (", ".join(ev_files) if ev_files else "none (clean run)"))
    out.append(bar)
    print("\n".join(out))
    return 0


def _discover(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    # Imported here so the replay path never touches the model client — a
    # property the hermetic zero-LLM test asserts on module level.
    from hands.discover import DiscoveryFailed, discover, load_request, report_as_json
    from hands.llm import LlmError, client_from_env

    try:
        request = load_request(args.request)
    except (OSError, ValueError) as exc:
        parser.error(f"cannot load discovery request: {exc}")
    try:
        model = client_from_env(model=args.model)
    except LlmError as exc:
        parser.error(str(exc))
    try:
        report = discover(request, model, runs_dir=args.runs_dir, headed=args.headed)
    except DiscoveryFailed as exc:
        print(f"discovery failed: {exc}", file=sys.stderr)
        return 1
    print(report_as_json(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
