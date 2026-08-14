"""Discovery orchestration: contract-first.

The caller-facing contract — parameters, outputs, outcome codes — is declared
UP FRONT in a discovery request; the LLM's job is to fill in the steps, never
to invent the contract. One run per ending: a happy run yields steps,
checkpoint, and outputs; one run per declared outcome yields a live-verified
recognizer (a recognizer for "member not found" can only be learned from a
run that actually saw a member not be found).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hands.artifact import (
    Checkpoint,
    Condition,
    OutputSpec,
    ParamSpec,
    RoleNameVisible,
    TargetApp,
    TargetLadder,
    dump_capability,
)
from hands.observe import build_observation
from hands.planner import Planner, PlannerModel, PlannerResult
from hands.recorder import (
    DistillationError,
    assemble,
    build_identity_checkpoint,
    build_outcome_condition,
    build_output,
    merge_regions,
)
from hands.surface import PlaywrightError, WebSurface
from hands.trace import Trace, new_run_dir


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OutputRequest(_Model):
    type: Literal["decimal", "string"]
    sensitive: bool = False


class OutcomeRequest(_Model):
    description: str
    params: dict[str, str]  # example values that should trigger this outcome
    identity_param: str  # which parameter identifies the record being asked about


class DiscoveryRequest(_Model):
    """The contract seed. Everything an agent needs to call the future
    capability is declared here; discovery only learns HOW."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str
    target: TargetApp
    goal_template: str
    parameters: dict[str, ParamSpec]
    example_params: dict[str, str]
    identity_param: str
    outputs: dict[str, OutputRequest]
    outcomes: dict[str, OutcomeRequest]

    @model_validator(mode="after")
    def _coherent(self) -> DiscoveryRequest:
        if set(self.example_params) != set(self.parameters):
            raise ValueError("example_params must cover exactly the declared parameters")
        if self.identity_param not in self.parameters:
            raise ValueError("identity_param must be a declared parameter")
        for name, spec in self.parameters.items():
            if spec.sensitive and name in self.example_params:
                raise ValueError(
                    f"sensitive parameter {name!r} must not have a literal example value; "
                    f"discovery for sensitive flows supplies values via the environment"
                )
        for code, outcome in self.outcomes.items():
            if set(outcome.params) != set(self.parameters):
                raise ValueError(f"outcome {code!r} params must cover the declared parameters")
        return self


def load_request(path: Path) -> DiscoveryRequest:
    return DiscoveryRequest.model_validate_json(path.read_text())


@dataclass
class HappyParts:
    requires: list[RoleNameVisible]
    steps: list[dict[str, object]]
    step_ids: list[str]
    checkpoint: object
    outputs: dict[str, OutputSpec]
    regions: dict[str, TargetLadder]


@dataclass
class RunSummary:
    ending: str
    steps: int
    llm_calls: int
    prompt_tokens: int
    completion_tokens: int
    invalid_proposals: int
    duration_s: float


@dataclass
class DiscoveryReport:
    artifact_path: Path | None
    happy: RunSummary
    outcome_runs: dict[str, RunSummary]
    error: str | None = None


class DiscoveryFailed(Exception):
    def __init__(self, message: str, report: DiscoveryReport | None = None) -> None:
        super().__init__(message)
        self.report = report


def discover(
    request: DiscoveryRequest,
    model: PlannerModel,
    *,
    runs_dir: Path = Path("runs"),
    out_dir: Path = Path("capabilities/generated"),
    headed: bool = False,
) -> DiscoveryReport:
    goal = request.goal_template
    param_notes = "\n".join(
        f"- {{param:{name}}}: {spec.description}"
        + ("" if spec.sensitive else f" (this run uses: {request.example_params.get(name, '')})")
        for name, spec in request.parameters.items()
    )
    outcome_desc = {code: o.description for code, o in request.outcomes.items()}

    # ---- happy run: steps + checkpoint + outputs
    happy, parts = _run_happy(
        request, model, goal, param_notes, outcome_desc, runs_dir, headed
    )

    # ---- one run per declared outcome: a live-verified recognizer each
    conditions: list[Condition] = []
    outcome_summaries: dict[str, RunSummary] = {}
    for code, outcome_request in request.outcomes.items():
        condition, new_regions, summary = _run_outcome(
            request, model, code, outcome_request, param_notes, outcome_desc,
            parts.step_ids, runs_dir, headed,
        )
        merge_regions(parts.regions, new_regions, where=f"outcome {code}")
        conditions.append(condition)
        outcome_summaries[code] = summary

    assert isinstance(parts.checkpoint, Checkpoint)
    capability = assemble(
        name=request.name,
        description=request.description,
        target=request.target.model_dump(),
        requires=parts.requires,
        parameters={k: v.model_dump() for k, v in request.parameters.items()},
        outcomes={code: {"description": o.description} for code, o in request.outcomes.items()},
        outputs=parts.outputs,
        regions=parts.regions,
        steps=parts.steps,
        conditions=conditions,
        checkpoint=parts.checkpoint,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = out_dir / f"{request.name}.json"
    artifact_path.write_text(dump_capability(capability))
    return DiscoveryReport(
        artifact_path=artifact_path, happy=happy, outcome_runs=outcome_summaries
    )


def _run_happy(
    request: DiscoveryRequest,
    model: PlannerModel,
    goal: str,
    param_notes: str,
    outcome_desc: dict[str, str],
    runs_dir: Path,
    headed: bool,
) -> tuple[RunSummary, HappyParts]:
    import time as _time

    run_dir = new_run_dir(runs_dir, f"discover-{request.name}")
    surface = WebSurface(headed=headed)
    started = _time.monotonic()
    with Trace(run_dir) as trace:
        trace.emit("discovery_started", capability=request.name, leg="happy")
        try:
            surface.start(request.target.entry.web.url)
            requires = _entry_requires(surface)
            planner = Planner(model=model)
            result = planner.run(
                surface, trace, goal, request.example_params, param_notes, outcome_desc
            )
            summary = _summarize(result, _time.monotonic() - started)
            if result.ending != "done" or result.done is None:
                raise DiscoveryFailed(
                    f"happy run ended {result.ending!r} "
                    f"({result.stuck_reason or 'no further detail'})",
                    DiscoveryReport(None, summary, {}),
                )
            context = result.steps[-1].target["context"] if result.steps else []
            checkpoint, regions = build_identity_checkpoint(
                surface,
                result.done.checkpoint_heading,
                result.done.identity_anchor_text,
                request.identity_param,
                request.example_params,
                _contexts(context),
            )
            outputs: dict[str, OutputSpec] = {}
            proposed = {o["name"]: o["anchor_text"] for o in result.done.outputs}
            for name, spec in request.outputs.items():
                anchor = proposed.get(name)
                if anchor is None:
                    raise DistillationError(
                        f"the run finished without locating requested output {name!r}"
                    )
                output, new_regions = build_output(
                    surface, name, anchor, spec.type, spec.sensitive, _contexts(context)
                )
                merge_regions(regions, new_regions, where=f"output {name}")
                outputs[name] = output
            steps: list[dict[str, object]] = [
                {
                    "id": s.id,
                    "intent": s.intent,
                    "action": s.action,
                    "target": s.target,
                    "pre": [p.model_dump() for p in s.pre],
                    "post": [p.model_dump() for p in s.post],
                    "risk": s.risk,
                }
                for s in result.steps
            ]
            trace.emit("discovery_distilled", steps=len(steps), outputs=sorted(outputs))
            return summary, HappyParts(
                requires=requires,
                steps=steps,
                step_ids=[s.id for s in result.steps],
                checkpoint=checkpoint,
                outputs=outputs,
                regions=regions,
            )
        except DistillationError as exc:
            raise DiscoveryFailed(f"distillation failed: {exc}") from exc
        finally:
            surface.stop()


def _run_outcome(
    request: DiscoveryRequest,
    model: PlannerModel,
    code: str,
    outcome_request: OutcomeRequest,
    param_notes: str,
    outcome_desc: dict[str, str],
    happy_step_ids: list[str],
    runs_dir: Path,
    headed: bool,
) -> tuple[Condition, dict[str, TargetLadder], RunSummary]:
    import time as _time

    run_dir = new_run_dir(runs_dir, f"discover-{request.name}-{code.lower()}")
    surface = WebSurface(headed=headed)
    started = _time.monotonic()
    with Trace(run_dir) as trace:
        trace.emit("discovery_started", capability=request.name, leg=f"outcome:{code}")
        try:
            surface.start(request.target.entry.web.url)
            goal = (
                f"{request.goal_template}\nThis run's inputs are expected to produce the "
                f"business outcome {code} ({outcome_request.description}). When the "
                f"application shows that outcome, call report_outcome."
            )
            planner = Planner(model=model)
            result = planner.run(
                surface, trace, goal, outcome_request.params, param_notes, outcome_desc
            )
            summary = _summarize(result, _time.monotonic() - started)
            if result.ending != "outcome" or result.outcome is None:
                raise DiscoveryFailed(
                    f"outcome run for {code} ended {result.ending!r} "
                    f"({result.stuck_reason or 'no further detail'})"
                )
            if result.outcome.code != code:
                raise DiscoveryFailed(
                    f"outcome run for {code} reported {result.outcome.code!r} instead"
                )
            # Arm the recognizer at the same position in the happy flow that
            # this run had reached when the outcome appeared.
            acted = len(result.steps)
            if acted == 0 or not happy_step_ids:
                raise DiscoveryFailed(f"outcome run for {code} acted no steps; cannot arm")
            armed_after = happy_step_ids[min(acted, len(happy_step_ids)) - 1]
            context = result.steps[-1].target["context"]
            condition, regions = build_outcome_condition(
                surface,
                code,
                result.outcome.marker_text,
                result.outcome.region_anchor_text,
                armed_after,
                outcome_request.params,
                _contexts(context),
            )
            trace.emit("discovery_distilled", condition=condition.id, armed_after=armed_after)
            return condition, regions, summary
        except DistillationError as exc:
            raise DiscoveryFailed(f"distillation failed for outcome {code}: {exc}") from exc
        finally:
            surface.stop()


def _entry_requires(surface: WebSurface) -> list[RoleNameVisible]:
    """The entry-state contract: the first visible heading on the entry page.
    Replay verifies it before step 1 (PRECONDITION_FAILED, not a locator error,
    when the session lands somewhere unexpected)."""
    observation = build_observation(surface)
    for view in observation.frames:
        try:
            frame = surface.frame_for(view.context)
            headings = frame.get_by_role("heading")
            for i in range(headings.count()):
                nth = headings.nth(i)
                if nth.is_visible():
                    return [
                        RoleNameVisible(
                            role="heading",
                            name=nth.inner_text().strip(),
                            context=view.context,
                        )
                    ]
        except PlaywrightError:
            continue
    return []


def _contexts(raw: object) -> list:  # type: ignore[type-arg]
    from hands.artifact import ContextSegment

    if not isinstance(raw, list):
        return []
    return [ContextSegment.model_validate(seg) for seg in raw]


def _summarize(result: PlannerResult, duration_s: float) -> RunSummary:
    return RunSummary(
        ending=result.ending,
        steps=len(result.steps),
        llm_calls=result.llm_calls,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        invalid_proposals=result.invalid_proposals,
        duration_s=round(duration_s, 2),
    )


def report_as_json(report: DiscoveryReport) -> str:
    return json.dumps(
        {
            "artifact": str(report.artifact_path) if report.artifact_path else None,
            "happy": report.happy.__dict__,
            "outcomes": {k: v.__dict__ for k, v in report.outcome_runs.items()},
        },
        indent=2,
    )
