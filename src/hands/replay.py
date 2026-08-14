"""The replay engine: the production execution path.

Consumes a capability artifact and invocation parameters; drives the surface
deterministically; returns exactly one of the contract's result shapes. No
model is involved anywhere in this module or anything it imports — that
property is enforced by a hermetic test, not by convention.

Timeout model (two levels, deliberately):
- Playwright's own action auto-waiting gets a SHORT per-attempt timeout;
- the engine owns a per-step budget and runs act as a bounded retry loop,
  checking recognizers before the first attempt and between attempts. A known
  interstitial that appears between steps is recognized and classified instead
  of being smashed into one long opaque TimeoutError.

Retry rules: a step marked risky is never re-attempted — a click that timed
out may still have landed server-side, and double-firing an irreversible
action is the one failure this engine must make structurally impossible.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from hands.artifact import (
    Capability,
    Condition,
    Step,
    TypeAction,
)
from hands.conditions import (
    check_recognizers,
    describe_state,
    matching_now,
    post_holds,
    state_holds,
)
from hands.results import (
    BusinessOutcome,
    Failure,
    FailureReport,
    OutputValue,
    PreconditionFailed,
    ReplayResult,
    Success,
)
from hands.surface import (
    PlaywrightError,
    PlaywrightTimeoutError,
    ResolvedTarget,
    SurfaceError,
    WebSurface,
)
from hands.trace import Trace, new_run_dir
from hands.values import ValueError_, parse_money, resolve_text


@dataclass
class EngineConfig:
    headed: bool = False
    runs_dir: Path = Path("runs")
    step_budget_s: float = 10.0
    requires_budget_s: float = 3.0
    poll_interval_s: float = 0.25
    attempt_timeout_ms: int = 2000


class _Recognized(Exception):
    """Internal control flow: an armed recognizer classified the state."""

    def __init__(self, outcome: BusinessOutcome) -> None:
        self.outcome = outcome


class _StepFailed(Exception):
    def __init__(self, report: FailureReport) -> None:
        self.report = report


class ReplayEngine:
    def __init__(self, config: EngineConfig | None = None) -> None:
        self.config = config or EngineConfig()

    def run(self, capability: Capability, params: dict[str, str]) -> ReplayResult:
        self._validate_params(capability, params)
        run_dir = new_run_dir(self.config.runs_dir, capability.name)
        surface = WebSurface(
            headed=self.config.headed, attempt_timeout_ms=self.config.attempt_timeout_ms
        )
        with Trace(run_dir) as trace:
            trace.emit(
                "run_started",
                capability=capability.name,
                version=capability.version,
                params=_masked_params(capability, params),
            )
            try:
                surface.start(capability.target.entry.web.url)
                result = self._execute(capability, params, surface, trace, run_dir)
            except _Recognized as hit:
                result = hit.outcome
            except _StepFailed as failed:
                shot, snap = surface.capture_evidence(run_dir)
                failed.report.screenshot_path = shot
                failed.report.snapshot_path = snap
                result = Failure(report=failed.report)
            finally:
                surface.stop()
            trace.emit("run_finished", result=_masked_result(capability, result))
        return result

    # ------------------------------------------------------------------ flow

    def _execute(
        self,
        capability: Capability,
        params: dict[str, str],
        surface: WebSurface,
        trace: Trace,
        run_dir: Path,
    ) -> ReplayResult:
        unmet = self._check_requires(capability, params, surface, trace)
        if unmet is not None:
            return unmet

        step_order = {step.id: i for i, step in enumerate(capability.steps)}
        for index, step in enumerate(capability.steps):
            armed = [
                c for c in capability.conditions if step_order[c.armed_after] <= index
            ]
            self._run_step(capability, params, surface, trace, step, armed)

        self._verify_checkpoint(capability, params, surface, trace)
        outputs = self._extract_outputs(capability, surface, trace)
        return Success(outputs=outputs)

    def _check_requires(
        self,
        capability: Capability,
        params: dict[str, str],
        surface: WebSurface,
        trace: Trace,
    ) -> PreconditionFailed | None:
        deadline = time.monotonic() + self.config.requires_budget_s
        pending = list(capability.requires)
        while pending and time.monotonic() < deadline:
            pending = [
                c for c in pending if not state_holds(surface, c, capability, params)
            ]
            if pending:
                time.sleep(self.config.poll_interval_s)
        if pending:
            described = "; ".join(describe_state(c) for c in pending)
            trace.emit("requires_unmet", unmet=described, url=surface.page.url)
            return PreconditionFailed(unmet=described, observed=surface.page.url)
        trace.emit("requires_met")
        return None

    def _run_step(
        self,
        capability: Capability,
        params: dict[str, str],
        surface: WebSurface,
        trace: Trace,
        step: Step,
        armed: list[Condition],
    ) -> None:
        trace.emit("step_started", step=step.id, intent=step.intent, risk=step.risk)
        deadline = time.monotonic() + self.config.step_budget_s

        # Freshness snapshot: recognizers already matching BEFORE this step's
        # action are stale state and may not classify this step's result.
        suppressed = matching_now(surface, capability, armed)
        if suppressed:
            trace.emit("recognizers_suppressed_stale", step=step.id, ids=sorted(suppressed))

        resolved = self._act_with_retries(
            capability, params, surface, trace, step, armed, suppressed, deadline
        )
        self._await_post(
            capability, params, surface, trace, step, armed, suppressed, resolved, deadline
        )

    def _act_with_retries(
        self,
        capability: Capability,
        params: dict[str, str],
        surface: WebSurface,
        trace: Trace,
        step: Step,
        armed: list[Condition],
        suppressed: set[str],
        deadline: float,
    ) -> ResolvedTarget | None:
        attempts = 0
        while True:
            self._poll_recognizers(capability, surface, trace, armed, suppressed)
            try:
                resolved = self._resolve_and_check_pre(surface, step)
                self._perform(surface, step, resolved, params)
                trace.emit(
                    "acted",
                    step=step.id,
                    action=step.action.kind,
                    rung=None if resolved is None else resolved.rung,
                    rung_index=None if resolved is None else resolved.rung_index,
                    text=_masked_action_text(capability, step, params),
                )
                return resolved
            except (SurfaceError, PlaywrightTimeoutError, PlaywrightError, ValueError_) as exc:
                attempts += 1
                retryable = (
                    step.risk == "safe"
                    and isinstance(exc, PlaywrightTimeoutError)
                    and time.monotonic() < deadline
                )
                trace.emit(
                    "act_attempt_failed",
                    step=step.id,
                    attempt=attempts,
                    error=str(exc),
                    will_retry=retryable,
                )
                if retryable:
                    time.sleep(self.config.poll_interval_s)
                    continue
                raise _StepFailed(
                    FailureReport(
                        step_id=step.id,
                        intent=step.intent,
                        expected=self._describe_target(step),
                        observed=str(exc),
                    )
                ) from exc

    def _resolve_and_check_pre(
        self, surface: WebSurface, step: Step
    ) -> ResolvedTarget | None:
        if step.target is None:
            return None
        resolved = surface.resolve(step.target)
        for pre in step.pre:
            ok = (
                resolved.locator.is_visible()
                if pre.kind == "visible"
                else resolved.locator.is_editable()
            )
            if not ok:
                raise SurfaceError(f"precondition {pre.kind} not met on resolved target")
        return resolved

    def _perform(
        self,
        surface: WebSurface,
        step: Step,
        resolved: ResolvedTarget | None,
        params: dict[str, str],
    ) -> None:
        action = step.action
        if isinstance(action, TypeAction):
            assert resolved is not None  # guaranteed by schema validator
            surface.type_text(resolved, resolve_text(action.text, params))
        elif action.kind == "click":
            assert resolved is not None
            surface.click(resolved)
        else:
            surface.goto(action.url)

    def _await_post(
        self,
        capability: Capability,
        params: dict[str, str],
        surface: WebSurface,
        trace: Trace,
        step: Step,
        armed: list[Condition],
        suppressed: set[str],
        resolved: ResolvedTarget | None,
        deadline: float,
    ) -> None:
        while True:
            # Precedence: a satisfied postcondition wins over a simultaneously
            # matching recognizer — the step demonstrably worked.
            if all(
                post_holds(surface, c, capability, params, resolved) for c in step.post
            ):
                trace.emit("postconditions_met", step=step.id)
                return
            self._poll_recognizers(capability, surface, trace, armed, suppressed)
            if time.monotonic() >= deadline:
                expected = " AND ".join(describe_state(c) for c in step.post)
                raise _StepFailed(
                    FailureReport(
                        step_id=step.id,
                        intent=step.intent,
                        expected=expected,
                        observed=f"postconditions still unmet at {surface.page.url}",
                    )
                )
            time.sleep(self.config.poll_interval_s)

    def _poll_recognizers(
        self,
        capability: Capability,
        surface: WebSurface,
        trace: Trace,
        armed: list[Condition],
        suppressed: set[str],
    ) -> None:
        hit = check_recognizers(surface, capability, armed, suppressed)
        if hit is None:
            return
        trace.emit(
            "recognizer_fired",
            condition=hit.condition.id,
            outcome=hit.condition.outcome_code,
            matched=hit.evidence.matched_text,
        )
        raise _Recognized(
            BusinessOutcome(
                code=hit.condition.outcome_code,
                description=capability.outcomes[hit.condition.outcome_code].description,
                evidence=hit.evidence,
            )
        )

    def _verify_checkpoint(
        self,
        capability: Capability,
        params: dict[str, str],
        surface: WebSurface,
        trace: Trace,
    ) -> None:
        deadline = time.monotonic() + self.config.step_budget_s
        armed = list(capability.conditions)
        while True:
            pending = [
                c
                for c in capability.checkpoint.all
                if not state_holds(surface, c, capability, params)
            ]
            if not pending:
                trace.emit("checkpoint_verified")
                return
            self._poll_recognizers(capability, surface, trace, armed, set())
            if time.monotonic() >= deadline:
                expected = " AND ".join(describe_state(c) for c in pending)
                raise _StepFailed(
                    FailureReport(
                        step_id=None,
                        intent="verify checkpoint",
                        expected=expected,
                        observed=f"checkpoint unmet at {surface.page.url}",
                    )
                )
            time.sleep(self.config.poll_interval_s)

    def _extract_outputs(
        self, capability: Capability, surface: WebSurface, trace: Trace
    ) -> dict[str, OutputValue]:
        outputs: dict[str, OutputValue] = {}
        for name, spec in capability.outputs.items():
            try:
                within = (
                    surface.resolve_region(capability.regions[spec.region])
                    if spec.region is not None
                    else None
                )
                resolved = surface.resolve(spec.source, within=within)
                raw = resolved.locator.inner_text(timeout=1000).strip()
            except (SurfaceError, PlaywrightError) as exc:
                raise _StepFailed(
                    FailureReport(
                        step_id=None,
                        intent=f"extract output {name!r}",
                        expected=f"a value for {name!r}",
                        observed=str(exc),
                    )
                ) from exc
            value: OutputValue
            if spec.parse.kind == "money":
                try:
                    value = parse_money(raw)
                except ValueError_ as exc:
                    raise _StepFailed(
                        FailureReport(
                            step_id=None,
                            intent=f"extract output {name!r}",
                            expected="a parseable money value",
                            observed=f"raw text {raw!r}",
                        )
                    ) from exc
            else:
                value = raw
            outputs[name] = value
            trace.emit(
                "output_extracted",
                name=name,
                value="«masked»" if spec.sensitive else str(value),
            )
        return outputs

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _validate_params(capability: Capability, params: dict[str, str]) -> None:
        declared = set(capability.parameters)
        given = set(params)
        if missing := declared - given:
            raise ValueError(f"missing parameters: {sorted(missing)}")
        if extra := given - declared:
            raise ValueError(f"unknown parameters: {sorted(extra)}")
        for name, spec in capability.parameters.items():
            if spec.pattern is not None and not re.fullmatch(spec.pattern, params[name]):
                raise ValueError(f"parameter {name!r} does not match pattern {spec.pattern!r}")

    @staticmethod
    def _describe_target(step: Step) -> str:
        if step.target is None:
            return step.action.kind
        from hands.surface import describe_rung

        rungs = " -> ".join(describe_rung(r) for r in step.target.ladder)
        return f"a unique visible target via ladder [{rungs}]"


def _masked_params(capability: Capability, params: dict[str, str]) -> dict[str, str]:
    return {
        k: "«masked»" if capability.parameters[k].sensitive else v for k, v in params.items()
    }


def _masked_action_text(
    capability: Capability, step: Step, params: dict[str, str]
) -> str | None:
    if not isinstance(step.action, TypeAction):
        return None
    masked = {
        k: "«masked»" if capability.parameters[k].sensitive else v for k, v in params.items()
    }
    return resolve_text(step.action.text, masked)


def _masked_result(capability: Capability, result: ReplayResult) -> dict[str, object]:
    if isinstance(result, Success):
        masked_outputs: dict[str, object] = {}
        for name, value in result.outputs.items():
            spec = capability.outputs[name]
            masked_outputs[name] = "«masked»" if spec.sensitive else _plain(value)
        return {"result": "success", "outputs": masked_outputs}
    return result.model_dump(mode="json")


def _plain(value: OutputValue) -> str:
    return str(value) if isinstance(value, Decimal) else value
