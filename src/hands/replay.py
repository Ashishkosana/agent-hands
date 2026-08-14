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

import hashlib
import json
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
    ValueMatchesParam,
    dump_capability,
)
from hands.conditions import (
    RecognizerHit,
    check_recognizers,
    describe_state,
    matching_now,
    post_holds,
    state_holds,
)
from hands.escalation import (
    ConsoleServer,
    ControlState,
    Decision,
    EscalationHub,
    HumanCommand,
    Intervention,
)
from hands.results import (
    BusinessOutcome,
    Failure,
    FailureReport,
    MatchEvidence,
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
class EscalationSettings:
    """Attended mode: failures pause the run and raise an intervention for a
    human instead of returning FAILURE immediately."""

    ttl_s: float = 300.0  # unanswered interventions fail; the session is closed
    console_port: int = 0  # 0 = ephemeral
    max_escalations: int = 3  # per run; beyond this, fail rather than ping-pong


@dataclass
class EngineConfig:
    headed: bool = False
    runs_dir: Path = Path("runs")
    step_budget_s: float = 10.0
    requires_budget_s: float = 3.0
    poll_interval_s: float = 0.25
    attempt_timeout_ms: int = 2000
    escalation: EscalationSettings | None = None


class _Recognized(Exception):
    """Internal control flow: an armed recognizer classified the state."""

    def __init__(self, outcome: BusinessOutcome) -> None:
        self.outcome = outcome


class _StepFailed(Exception):
    def __init__(self, report: FailureReport) -> None:
        self.report = report


class _PauseRequested(Exception):
    """Internal control flow: the operator asked for control mid-run."""


class _Recover(Exception):
    """Internal control flow: an armed RECOVERABLE recognizer matched.
    ``acted`` records whether the current step's action had already run —
    the difference between a safe retry and a forbidden risky re-fire."""

    def __init__(self, hit: RecognizerHit, acted: bool) -> None:
        self.hit = hit
        self.acted = acted


class ReplayEngine:
    def __init__(self, config: EngineConfig | None = None) -> None:
        self.config = config or EngineConfig()
        self._hub: EscalationHub | None = None

    def run(self, capability: Capability, params: dict[str, str]) -> ReplayResult:
        self._validate_params(capability, params)
        run_dir = new_run_dir(self.config.runs_dir, capability.name)
        surface = WebSurface(
            headed=self.config.headed, attempt_timeout_ms=self.config.attempt_timeout_ms
        )
        hub: EscalationHub | None = None
        console: ConsoleServer | None = None
        with Trace(run_dir) as trace:
            trace.emit(
                "run_started",
                capability=capability.name,
                version=capability.version,
                # The hash of the exact artifact that ran: every trace line is
                # attributable to one reviewable contract.
                artifact_sha256=hashlib.sha256(
                    dump_capability(capability).encode()
                ).hexdigest(),
                params=_masked_params(capability, params),
            )
            on_human_event = None
            if self.config.escalation is not None:
                hub = EscalationHub()
                console = ConsoleServer(hub, port=self.config.escalation.console_port)
                console.start()
                trace.emit("operator_console_started", url=f"http://127.0.0.1:{console.port}/")

                def on_human_event(event: dict[str, object], _hub: EscalationHub = hub) -> None:
                    if _hub.record_human_action(event):
                        trace.emit("human_action", **event)

            self._hub = hub
            try:
                surface.start(capability.target.entry.web.url, on_human_event=on_human_event)
                result = self._execute(capability, params, surface, trace, run_dir)
            except _Recognized as hit:
                result = hit.outcome
            except _StepFailed as failed:
                # Page-content evidence is suppressed once a human has driven
                # the session: what they entered may still sit in page state,
                # and a screenshot/snapshot would persist it. (Element-level
                # taint masking would refine suppression to masking.)
                if hub is not None and (
                    hub.state is ControlState.HUMAN or hub.human_actions
                ):
                    trace.emit("evidence_suppressed_human_window")
                else:
                    shot, snap = surface.capture_evidence(run_dir)
                    failed.report.screenshot_path = shot
                    failed.report.snapshot_path = snap
                result = Failure(report=failed.report)
            finally:
                surface.stop()
                if console is not None:
                    console.stop()
                self._hub = None
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
        fires: dict[str, int] = {}
        suppressed: set[str] = set()
        escalations = 0
        total = len(capability.steps)
        index = 0
        # The index loop exists for resume semantics: a recoverable condition
        # (or a human handback) may retry the current step or resume anywhere,
        # bounded by fire caps and the escalation budget. index == total is
        # the checkpoint phase (a pseudo-step recovery may also resume into).
        while index <= total:
            step = capability.steps[index] if index < total else None
            try:
                if step is None:
                    self._verify_checkpoint(capability, params, surface, trace, suppressed)
                    break
                armed = [
                    c for c in capability.conditions if step_order[c.armed_after] <= index
                ]
                suppressed = self._run_step(capability, params, surface, trace, step, armed)
                index += 1
            except _Recover as rec:
                try:
                    index = self._recover(
                        capability, params, surface, trace, rec, fires, step_order,
                        current=index, step=step,
                    )
                except _StepFailed as failed:
                    index, escalations = self._maybe_escalate(
                        capability, params, surface, trace, run_dir,
                        failed.report, index, escalations,
                    )
            except _StepFailed as failed:
                index, escalations = self._maybe_escalate(
                    capability, params, surface, trace, run_dir,
                    failed.report, index, escalations,
                )
            except _PauseRequested:
                index, escalations = self._maybe_escalate(
                    capability, params, surface, trace, run_dir,
                    FailureReport(
                        step_id=step.id if step else None,
                        intent=step.intent if step else "verify checkpoint",
                        expected="operator requested control",
                        observed="run paused at the operator's request",
                    ),
                    index, escalations,
                )

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
    ) -> set[str]:
        trace.emit("step_started", step=step.id, intent=step.intent, risk=step.risk)
        if step.target is not None and step.target.fragile:
            trace.emit("fragile_target_warning", step=step.id)
        deadline = time.monotonic() + self.config.step_budget_s

        # Freshness snapshot (business outcomes only): a recognizer already
        # matching BEFORE this step's action is stale state and may not
        # classify the step's result. Suppression lifts once the stale state
        # is observed GONE. Recoverable conditions are never suppressed — a
        # blocking state present before the action is exactly what recovery
        # exists for.
        suppressed = matching_now(
            surface, capability, [c for c in armed if c.classify == "business_outcome"]
        )
        if suppressed:
            trace.emit("recognizers_suppressed_stale", step=step.id, ids=sorted(suppressed))

        resolved = self._act_with_retries(
            capability, params, surface, trace, step, armed, suppressed, deadline
        )
        self._await_post(
            capability, params, surface, trace, step, armed, suppressed, resolved, deadline
        )
        return suppressed

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
            self._refresh_suppression(capability, surface, trace, armed, suppressed)
            self._poll_recognizers(capability, surface, trace, armed, suppressed, acted=False)
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
                    # Probe for the action's effect before re-acting: if this
                    # step's (state) postconditions already hold, the action
                    # landed and re-acting would double-fire it.
                    if self._effect_already_present(capability, params, surface, step):
                        trace.emit("action_effect_detected", step=step.id, attempt=attempts)
                        return None
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
            self._refresh_suppression(capability, surface, trace, armed, suppressed)
            self._poll_recognizers(capability, surface, trace, armed, suppressed, acted=True)
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
        acted: bool,
    ) -> None:
        if (
            self._hub is not None
            and self._hub.pause_requested
            and self._hub.state is ControlState.AUTOMATION
        ):
            raise _PauseRequested
        hit = check_recognizers(surface, capability, armed, suppressed)
        if hit is None:
            return
        if hit.condition.classify == "recoverable":
            raise _Recover(hit, acted=acted)
        code = hit.condition.outcome_code
        assert code is not None  # guaranteed by the schema validator
        trace.emit(
            "recognizer_fired",
            condition=hit.condition.id,
            outcome=code,
            matched=hit.evidence.matched_text,
        )
        raise _Recognized(
            BusinessOutcome(
                code=code,
                description=capability.outcomes[code].description,
                evidence=hit.evidence,
            )
        )

    def _recover(
        self,
        capability: Capability,
        params: dict[str, str],
        surface: WebSurface,
        trace: Trace,
        rec: _Recover,
        fires: dict[str, int],
        step_order: dict[str, int],
        current: int,
        step: Step | None,
    ) -> int:
        """Handle a recoverable condition: bounded, deliberate, loud on
        exhaustion. Returns the step index to resume at."""
        cond = rec.hit.condition
        count = fires.get(cond.id, 0) + 1
        fires[cond.id] = count
        trace.emit(
            "recognizer_fired",
            condition=cond.id,
            classify="recoverable",
            matched=rec.hit.evidence.matched_text,
            fire=count,
        )
        if count > cond.max_fires_per_run:
            raise _StepFailed(
                FailureReport(
                    step_id=step.id if step is not None else None,
                    intent=f"recover from condition {cond.id!r}",
                    expected=f"at most {cond.max_fires_per_run} recoveries per run",
                    observed=f"condition {cond.id!r} fired {count} times — recovery cap exhausted",
                )
            )
        if rec.acted and step is not None and step.risk == "risky":
            # Re-running the step after its action may already have landed
            # server-side would double-fire an irreversible action. Recovery
            # cannot make that safe; a human can (escalation, next slice).
            raise _StepFailed(
                FailureReport(
                    step_id=step.id,
                    intent=step.intent,
                    expected="a risky step is never auto-retried after its action ran",
                    observed=f"recoverable condition {cond.id!r} interrupted a risky step "
                    f"post-action; escalation to a human is the safe path",
                )
            )
        self._run_recovery(capability, params, surface, trace, cond)
        if cond.resume == "retry_current_step":
            return current
        assert cond.restart_from is not None  # guaranteed by the schema validator
        return step_order[cond.restart_from]

    def _run_recovery(
        self,
        capability: Capability,
        params: dict[str, str],
        surface: WebSurface,
        trace: Trace,
        cond: Condition,
    ) -> None:
        """Execute a condition's recovery steps: single-attempt actions,
        bounded postcondition waits, and NO recognizer matching (one level of
        recovery, never nested). Any problem promotes to a hard FAILURE that
        names the original condition — the fourth, unnamed state the taxonomy
        must not have."""
        for rstep in cond.recovery:
            trace.emit(
                "recovery_step_started", condition=cond.id, step=rstep.id, intent=rstep.intent
            )
            try:
                resolved = self._resolve_and_check_pre(surface, rstep)
                self._perform(surface, rstep, resolved, params)
            except (SurfaceError, PlaywrightError, ValueError_) as exc:
                raise _StepFailed(
                    FailureReport(
                        step_id=rstep.id,
                        intent=f"recovery for {cond.id!r}: {rstep.intent}",
                        expected=self._describe_target(rstep),
                        observed=str(exc),
                    )
                ) from exc
            deadline = time.monotonic() + self.config.step_budget_s
            while not all(
                post_holds(surface, c, capability, params, resolved) for c in rstep.post
            ):
                if time.monotonic() >= deadline:
                    expected = " AND ".join(describe_state(c) for c in rstep.post)
                    raise _StepFailed(
                        FailureReport(
                            step_id=rstep.id,
                            intent=f"recovery for {cond.id!r}: {rstep.intent}",
                            expected=expected,
                            observed=f"recovery postconditions unmet at {surface.page.url}",
                        )
                    )
                time.sleep(self.config.poll_interval_s)
            trace.emit("recovery_step_done", condition=cond.id, step=rstep.id)

    def _refresh_suppression(
        self,
        capability: Capability,
        surface: WebSurface,
        trace: Trace,
        armed: list[Condition],
        suppressed: set[str],
    ) -> None:
        """Lift suppression for stale recognizers whose state has disappeared.

        The freshness rule has two halves: a match present before the action
        may not classify it (the snapshot), and once that stale state is seen
        GONE, the recognizer is live again — otherwise a genuine new match of
        the same condition within this step would be invisible.
        """
        if not suppressed:
            return
        still = matching_now(
            surface, capability, [c for c in armed if c.id in suppressed]
        )
        lifted = suppressed - still
        if lifted:
            trace.emit("recognizer_suppression_lifted", ids=sorted(lifted))
            suppressed.intersection_update(still)

    def _effect_already_present(
        self,
        capability: Capability,
        params: dict[str, str],
        surface: WebSurface,
        step: Step,
    ) -> bool:
        if not step.post:
            return False
        if any(isinstance(c, ValueMatchesParam) for c in step.post):
            # Value postconditions need the resolved target, which the failed
            # attempt may not have produced; don't guess.
            return False
        return all(post_holds(surface, c, capability, params, None) for c in step.post)

    def _maybe_escalate(
        self,
        capability: Capability,
        params: dict[str, str],
        surface: WebSurface,
        trace: Trace,
        run_dir: Path,
        report: FailureReport,
        index: int,
        escalations: int,
    ) -> tuple[int, int]:
        """Attended mode turns a would-be FAILURE into an intervention.
        Returns (resume index, escalation count); raises the terminal result
        when the operator aborts/resolves, the TTL expires, unattended mode
        has no human to ask, or the escalation budget is spent."""
        hub = self._hub
        settings = self.config.escalation
        if hub is None or settings is None:
            raise _StepFailed(report)
        escalations += 1
        if escalations > settings.max_escalations:
            report.observed += " (escalation budget exhausted)"
            raise _StepFailed(report)

        shot, _snap = surface.capture_evidence(run_dir)
        step = capability.steps[index] if index < len(capability.steps) else None
        intervention = Intervention(
            capability=capability.name,
            version=capability.version,
            step_id=report.step_id,
            intent=report.intent,
            reason=f"expected {report.expected}; observed {report.observed}",
            params=_masked_params(capability, params),
            expected=report.expected,
            remaining_steps=[s.intent for s in capability.steps[index:]],
            recent_events=trace.tail(),
            screenshot_path=shot,
        )
        (run_dir / f"intervention-{escalations}.json").write_text(
            json.dumps(intervention.__dict__, indent=2, default=str)
        )
        hub.park(intervention)
        trace.emit(
            "escalation_raised",
            step=report.step_id,
            reason=intervention.reason,
            escalation=escalations,
        )
        decision, note, code, operator = self._park_and_wait(
            capability, params, surface, trace, hub, settings
        )
        if decision is Decision.ABORT:
            trace.emit("escalation_aborted", operator=operator, reason=note)
            raise _StepFailed(
                FailureReport(
                    step_id=report.step_id,
                    intent=report.intent,
                    expected=report.expected,
                    observed=f"aborted by operator {operator!r}: {note}",
                )
            )
        if decision is Decision.RESOLVE:
            trace.emit("escalation_resolved", operator=operator, code=code, note=note)
            raise _Recognized(
                BusinessOutcome(
                    code=code,
                    description=capability.outcomes[code].description,
                    evidence=MatchEvidence(
                        condition_id=f"human:{operator}", matched_text=note or code
                    ),
                )
            )
        # APPROVE or HANDBACK: find where the flow actually is now. A human
        # commonly finishes the screen they were on — blind current-step
        # retry would re-execute steps against a moved-on page.
        hub.resume_automation()
        resume_at = self._resume_scan(capability, params, surface, index, step)
        trace.emit(
            "resume_decision",
            operator=operator,
            decision=decision.value,
            resume_index=resume_at,
            of_total=len(capability.steps),
        )
        return resume_at, escalations

    def _park_and_wait(
        self,
        capability: Capability,
        params: dict[str, str],
        surface: WebSurface,
        trace: Trace,
        hub: EscalationHub,
        settings: EscalationSettings,
    ) -> tuple[Decision, str, str, str | None]:
        """The parked engine loop: the ONLY thread that may touch the browser,
        so it also executes the console's mock manual-control commands on the
        operator's behalf. An unanswered intervention expires (no keep-alive
        games with a banking session)."""
        deadline = time.monotonic() + settings.ttl_s
        last_state = hub.state
        while True:
            state = hub.state
            if state is ControlState.HUMAN and last_state is ControlState.PAUSED:
                trace.emit("control_granted", operator=hub.operator)
            last_state = state
            if state is ControlState.HUMAN:
                command = hub.pop_command()
                if command is not None:
                    self._execute_human_command(surface, trace, hub, command)
                    continue
            decision, note, code, operator = hub.take_decision()
            if decision is not Decision.NONE:
                if decision is Decision.RESOLVE and code not in capability.outcomes:
                    trace.emit("escalation_invalid_decision", code=code, operator=operator)
                    continue  # invalid code: stay parked, the console shows declared codes
                return decision, note, code, operator
            if time.monotonic() >= deadline:
                trace.emit("escalation_ttl_expired", ttl_s=settings.ttl_s)
                raise _StepFailed(
                    FailureReport(
                        step_id=None,
                        intent="await operator",
                        expected=f"an operator decision within {settings.ttl_s}s",
                        observed="intervention unanswered; session closed",
                    )
                )
            time.sleep(min(self.config.poll_interval_s, 0.1))

    def _execute_human_command(
        self,
        surface: WebSurface,
        trace: Trace,
        hub: EscalationHub,
        command: HumanCommand,
    ) -> None:
        """Execute one console command on the live session. Typed values are
        never recorded — the trace carries a masked length, same as any human
        keystroke."""
        try:
            target = None
            for frame in surface.page.frames:
                matches = frame.get_by_role(command.role, name=command.name, exact=True)  # type: ignore[arg-type]
                visible = [
                    matches.nth(i) for i in range(matches.count()) if matches.nth(i).is_visible()
                ]
                if len(visible) == 1:
                    target = visible[0]
                    break
            if target is None:
                raise SurfaceError(
                    f"no unique visible {command.role} named {command.name!r} in any frame"
                )
            if command.kind == "type":
                target.fill(command.text, timeout=3000)
            else:
                target.click(timeout=3000)
            trace.emit(
                "human_command_executed",
                kind=command.kind,
                role=command.role,
                name=command.name,
                text_masked_length=len(command.text) if command.kind == "type" else None,
                operator=hub.operator,
            )
        except (SurfaceError, PlaywrightError) as exc:
            trace.emit("human_command_failed", kind=command.kind, error=str(exc))

    def _resume_scan(
        self,
        capability: Capability,
        params: dict[str, str],
        surface: WebSurface,
        current: int,
        current_step: Step | None,
    ) -> int:
        """Forward position scan after a handback: checkpoint first (the human
        may have completed the whole flow), then the furthest step whose
        postconditions hold, else retry the current step — unless it is risky,
        where a blind retry could double-fire; that fails loudly instead."""
        total = len(capability.steps)
        if all(
            state_holds(surface, c, capability, params) for c in capability.checkpoint.all
        ):
            return total
        for i in reversed(range(total)):
            if self._step_post_holds(capability, params, surface, capability.steps[i]):
                return i + 1
        if current_step is not None and current_step.risk == "risky":
            raise _StepFailed(
                FailureReport(
                    step_id=current_step.id,
                    intent=current_step.intent,
                    expected="a resume point that does not re-run a risky step",
                    observed="no completed-step frontier found after handback; retrying a "
                    "risky step blind could double-fire it",
                )
            )
        return current

    def _step_post_holds(
        self,
        capability: Capability,
        params: dict[str, str],
        surface: WebSurface,
        step: Step,
    ) -> bool:
        resolved = None
        if step.target is not None and any(
            isinstance(c, ValueMatchesParam) for c in step.post
        ):
            try:
                resolved = surface.resolve(step.target)
            except (SurfaceError, PlaywrightError):
                return False
        return all(post_holds(surface, c, capability, params, resolved) for c in step.post)

    def _verify_checkpoint(
        self,
        capability: Capability,
        params: dict[str, str],
        surface: WebSurface,
        trace: Trace,
        suppressed: set[str],
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
            self._refresh_suppression(capability, surface, trace, armed, suppressed)
            self._poll_recognizers(capability, surface, trace, armed, suppressed, acted=True)
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
                    observed = (
                        f"unparseable value («masked», {len(raw)} chars)"
                        if spec.sensitive
                        else f"raw text {raw!r}"
                    )
                    raise _StepFailed(
                        FailureReport(
                            step_id=None,
                            intent=f"extract output {name!r}",
                            expected="a parseable money value",
                            observed=observed,
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
