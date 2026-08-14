"""Command-line interface.

Exit codes: 0 = the system worked (SUCCESS or a BUSINESS_OUTCOME — an answer
is not a malfunction); 1 = FAILURE or PRECONDITION_FAILED; 2 = usage error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import TypeAdapter

from hands.artifact import load_capability
from hands.replay import EngineConfig, ReplayEngine
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

    discover_cmd = sub.add_parser(
        "discover", help="LLM-driven discovery: learn a flow and record it as a capability"
    )
    discover_cmd.add_argument("request", type=Path, help="a discovery request JSON file")
    discover_cmd.add_argument("--headed", action="store_true", help="show the browser")
    discover_cmd.add_argument("--runs-dir", type=Path, default=Path("runs"))
    discover_cmd.add_argument("--model", default=None, help="override the configured model")

    args = parser.parse_args(argv)

    if args.command == "discover":
        return _discover(parser, args)

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

    engine = ReplayEngine(EngineConfig(headed=args.headed, runs_dir=args.runs_dir))
    try:
        result = engine.run(capability, params)
    except ValueError as exc:  # parameter validation: a caller bug, not a run result
        parser.error(str(exc))

    print(_RESULT_ADAPTER.dump_json(result, indent=2).decode())
    return 0 if isinstance(result, (Success, BusinessOutcome)) else 1


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
