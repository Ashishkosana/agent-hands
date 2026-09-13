"""The independent verifier: observe durable state, return observations.

Second of the three authorities in a verified run (executor / verifier /
reconciler). The verifier replays a READ-ONLY capability to the identity-
verified page of the record in question and records what the page shows.
It does not decide anything: "the share is on HOLD" is an observation; whether
that means the executor's action committed is the reconciler's judgment.

Independence, stated precisely (no more than the target actually provides):

- Fresh process, fresh browser, fresh session. ``VerifierRunner.observe``
  spawns ``python -m hands.verifier`` — a separate interpreter with its own
  Playwright instance. It has no handle to the executor's page, cookies, or
  in-memory state, and cannot be handed one.
- Read-only, enforced at the network layer, not by trusting step labels. A
  ``ReadOnlyGuard`` aborts any non-GET request on the verifier's surface
  except the sign-on POST (session establishment). An attempt is recorded in
  the report as a violation; the run does not silently continue as if it
  were read-only.
- Same credentials class. MERIDIAN's demo operators share one password and
  both roles can read member records; the verifier may sign on as a
  different operator, but that is a different *login*, not a stronger
  privilege boundary. Nothing here claims otherwise.
- Same target. The verifier observes the same system of record through the
  same UI. It cannot detect a system of record that lies to everyone.

What the verifier emits: a ``VerifierReport`` — ok/not ok, the raw
``Observation`` (share ids and statuses; balances and contact data are
deliberately NOT recorded), the replay result kind, the run directory
(hash-chained trace + evidence), and any read-only violations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from playwright.sync_api import Request as PwRequest
from playwright.sync_api import Route
from pydantic import BaseModel

from hands.artifact import (
    RelativeRung,
    TargetLadder,
    TextRung,
    dump_capability,
    load_capability,
)
from hands.chaos import ChaosMode, FaultInjector
from hands.replay import EngineConfig, ReplayEngine, masked_result
from hands.results import ReplayResult, Success
from hands.surface import RequestInterceptor, SurfaceError, WebSurface

# Session establishment on the MERIDIAN-shaped console: the one POST a
# read-only observer must be allowed to make.
SESSION_ESTABLISHMENT_RE = re.compile(r"/signon$")
SHARE_TABLE_HEADER = "Share ID"


class ShareStatus(BaseModel):
    share_id: str
    type: str
    status: str  # normalized: first token, upper-cased ("HOLD [HOLD]" -> "HOLD")
    status_raw: str


class Observation(BaseModel):
    """Raw perception of the member record's SHARES / BALANCES table. Facts,
    not judgments; no balances, no contact data."""

    kind: Literal["share_statuses"] = "share_statuses"
    member_number: str
    shares: list[ShareStatus]
    observed_at: str
    page_url: str

    def status_of(self, share_id: str) -> str | None:
        for share in self.shares:
            if share.share_id == share_id:
                return share.status
        return None


class VerifierReport(BaseModel):
    ok: bool
    observation: Observation | None
    result_kind: str | None
    result: dict[str, object] | None  # masked replay result, for the evidence bundle
    run_dir: str | None
    artifact_sha256: str | None
    read_only_violations: list[str]
    error: str | None
    elapsed_s: float
    operator_id: str | None


def observe_share_statuses(surface: WebSurface) -> dict[str, object]:
    """The observer installed on the verifier's replay: runs on the identity-
    verified MEMBER RECORD page. Reads the member number cell and the share
    table; refuses (raises) rather than returning a partial reading."""
    member_cell = surface.resolve(
        TargetLadder(
            ladder=[RelativeRung(relation="cell_right", anchor=TextRung(text="Member No.:"))]
        )
    )
    member_number = member_cell.locator.inner_text(timeout=1000).strip()
    rows = surface.read_table(SHARE_TABLE_HEADER)
    if len(rows) < 1:
        raise SurfaceError("share table has no header row")
    header = [c.strip() for c in rows[0]]
    try:
        i_id = header.index("Share ID")
        i_type = header.index("Type")
        i_status = header.index("Status")
    except ValueError as exc:
        raise SurfaceError(f"share table header lacks expected columns: {header}") from exc
    shares: list[dict[str, str]] = []
    for row in rows[1:]:
        if len(row) <= max(i_id, i_type, i_status):
            raise SurfaceError(f"share table row has {len(row)} cells; expected {len(header)}")
        raw = row[i_status]
        tokens = raw.split()
        if not tokens:
            raise SurfaceError(f"share {row[i_id]!r} has an empty status cell")
        shares.append(
            {
                "share_id": row[i_id],
                "type": row[i_type],
                "status": tokens[0].upper(),
                "status_raw": raw,
            }
        )
    observation = Observation(
        member_number=member_number,
        shares=[ShareStatus(**s) for s in shares],
        observed_at=datetime.now(tz=UTC).isoformat(timespec="milliseconds"),
        page_url=surface.page.url,
    )
    return observation.model_dump(mode="json")


class ReadOnlyGuard:
    """Network-layer read-only enforcement for the verifier's surface."""

    def __init__(self, allow: re.Pattern[str] = SESSION_ESTABLISHMENT_RE) -> None:
        self.allow = allow
        self.violations: list[str] = []

    def __call__(self, route: Route, request: PwRequest) -> bool:
        if request.method.upper() == "GET":
            return False
        if self.allow.search(request.url.split("?", 1)[0]):
            return False
        self.violations.append(f"{request.method.upper()} {request.url}")
        route.abort("accessdenied")
        return True


def compose(*interceptors: RequestInterceptor | None) -> RequestInterceptor:
    """First interceptor to handle a route wins."""
    active = [i for i in interceptors if i is not None]

    def run(route: Route, request: PwRequest) -> bool:
        return any(interceptor(route, request) for interceptor in active)

    return run


def observe_in_process(
    artifact: Path,
    params: dict[str, str],
    runs_dir: Path,
    chaos: ChaosMode | None = None,
    step_budget_s: float | None = None,
) -> VerifierReport:
    """Run the verifier replay in THIS process. ``VerifierRunner`` calls this
    from a fresh interpreter; tests may call it directly."""
    started = time.monotonic()
    capability = load_capability(artifact)
    guard = ReadOnlyGuard()
    injector = FaultInjector(chaos) if chaos is not None else None
    config = EngineConfig(runs_dir=runs_dir, interceptor=compose(guard, injector))
    if step_budget_s is not None:
        config.step_budget_s = step_budget_s
    engine = ReplayEngine(config)
    artifact_sha = hashlib.sha256(dump_capability(capability).encode()).hexdigest()
    try:
        result: ReplayResult = engine.run(capability, params, observe=observe_share_statuses)
    except ValueError as exc:
        return VerifierReport(
            ok=False, observation=None, result_kind=None, result=None,
            run_dir=str(engine.last_run_dir) if engine.last_run_dir else None,
            artifact_sha256=artifact_sha, read_only_violations=guard.violations,
            error=f"parameter error: {exc}", elapsed_s=time.monotonic() - started,
            operator_id=params.get("operator_id"),
        )
    observation: Observation | None = None
    error: str | None = None
    if isinstance(result, Success) and result.observation is not None:
        observation = Observation.model_validate(result.observation)
    else:
        error = f"verifier replay ended in {result.result}"
    if guard.violations:
        # A verifier that tried to write is not a verifier. Its reading is
        # void regardless of what it saw.
        observation = None
        error = f"read-only violation: {guard.violations}"
    return VerifierReport(
        ok=observation is not None,
        observation=observation,
        result_kind=result.result,
        result=masked_result(capability, result),
        run_dir=str(engine.last_run_dir) if engine.last_run_dir else None,
        artifact_sha256=artifact_sha,
        read_only_violations=guard.violations,
        error=error,
        elapsed_s=time.monotonic() - started,
        operator_id=params.get("operator_id"),
    )


class VerifierRunner:
    """Runs the verifier in a fresh interpreter (fresh browser, fresh session)
    and parses its report. Sensitive parameters travel to the child through
    ``HANDS_PARAM_<NAME>`` environment variables, never the command line."""

    def __init__(
        self,
        artifact: Path,
        params: dict[str, str],
        runs_dir: Path,
        sensitive: frozenset[str] = frozenset({"password"}),
        python: str = sys.executable,
        timeout_s: float = 240.0,
        step_budget_s: float | None = None,
    ) -> None:
        self.artifact = artifact
        self.params = params
        self.runs_dir = runs_dir
        self.sensitive = sensitive
        self.python = python
        self.timeout_s = timeout_s
        self.step_budget_s = step_budget_s

    def observe(self, chaos: ChaosMode | None = None) -> VerifierReport:
        started = time.monotonic()
        argv = [self.python, "-m", "hands.verifier", "--artifact", str(self.artifact),
                "--runs-dir", str(self.runs_dir)]
        env = dict(os.environ)
        for name, value in self.params.items():
            if name in self.sensitive:
                env[f"HANDS_PARAM_{name.upper()}"] = value
            else:
                argv += ["--param", f"{name}={value}"]
        if chaos is not None:
            argv += ["--chaos", chaos.value]
        if self.step_budget_s is not None:
            argv += ["--step-budget", str(self.step_budget_s)]
        try:
            proc = subprocess.run(
                argv, env=env, capture_output=True, text=True, timeout=self.timeout_s,
                cwd=str(Path(__file__).resolve().parents[2]),
            )
        except subprocess.TimeoutExpired:
            return self._failed("verifier subprocess timed out", started)
        if proc.returncode != 0 or not proc.stdout.strip():
            return self._failed(
                f"verifier subprocess exit {proc.returncode}: {proc.stderr.strip()[-800:]}",
                started,
            )
        try:
            report = VerifierReport.model_validate_json(proc.stdout.strip().splitlines()[-1])
        except ValueError as exc:
            return self._failed(f"unparseable verifier report: {exc}", started)
        return report

    def _failed(self, error: str, started: float) -> VerifierReport:
        return VerifierReport(
            ok=False, observation=None, result_kind=None, result=None, run_dir=None,
            artifact_sha256=None, read_only_violations=[], error=error,
            elapsed_s=time.monotonic() - started, operator_id=self.params.get("operator_id"),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hands.verifier")
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--runs-dir", type=Path, default=Path("runs"))
    parser.add_argument("--step-budget", type=float, default=None)
    parser.add_argument(
        "--chaos", choices=[ChaosMode.VERIFIER_FAILURE.value], default=None,
        help="test-only: break this verifier's own read path",
    )
    args = parser.parse_args(argv)
    params: dict[str, str] = {}
    for item in args.param:
        name, sep, value = item.partition("=")
        if not sep or not name:
            parser.error(f"--param expects NAME=VALUE, got {item!r}")
        params[name] = value
    capability = load_capability(args.artifact)
    for name, spec in capability.parameters.items():
        if spec.sensitive:
            env = os.environ.get(f"HANDS_PARAM_{name.upper()}")
            if env is not None:
                params[name] = env
    report = observe_in_process(
        args.artifact, params, args.runs_dir,
        chaos=ChaosMode(args.chaos) if args.chaos else None,
        step_budget_s=args.step_budget,
    )
    print(json.dumps(report.model_dump(mode="json")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
