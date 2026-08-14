"""The planner: an LLM-driven observe -> decide -> act loop.

The model cannot free-type actions. Every turn it must call exactly one tool,
and an action must reference a ref from the CURRENT observation — the planner
validates every proposal and answers an invalid one with a corrective tool
result rather than executing a guess. Sensitive parameter values are never
placed in the prompt: the model writes ``{param:name}`` and the surface
resolves it at act time.

Stop conditions: ``done`` (with checkpoint evidence), ``report_outcome`` (a
legitimate non-success ending — "no such member" is an answer at discovery
time too), ``stuck``, max steps, or too many invalid proposals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from hands.artifact import (
    PostCondition,
    PreCondition,
    RoleNameVisible,
    UrlMatches,
    ValueMatchesParam,
)
from hands.llm import LlmTurn, Message, ToolCall, ToolSpec
from hands.observe import Observation, build_observation
from hands.recorder import DistillationError, build_ladder, scrub_url_pattern
from hands.surface import PlaywrightError, WebSurface
from hands.trace import Trace
from hands.values import placeholders_in, resolve_text


class PlannerModel(Protocol):
    """What the planner needs from a model — satisfied by LlmClient and by
    the scripted fake used in offline tests."""

    def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LlmTurn: ...


TOOLS: list[ToolSpec] = [
    {
        "type": "function",
        "function": {
            "name": "act",
            "description": "Perform one action on an element from the current observation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["click", "type"]},
                    "ref": {"type": "string", "description": "an element ref, e.g. e3"},
                    "text": {
                        "type": "string",
                        "description": "for type: literal text or {param:name}",
                    },
                    "intent": {"type": "string", "description": "short human-readable intent"},
                },
                "required": ["kind", "ref", "intent"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "The goal is complete and the target state is on screen.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "checkpoint_heading": {
                        "type": "string",
                        "description": "the exact heading text that proves the goal state",
                    },
                    "identity_anchor_text": {
                        "type": "string",
                        "description": "exact label text next to the identifying value "
                        "(e.g. 'Member #') — NOT the value itself",
                    },
                    "outputs": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "anchor_text": {
                                    "type": "string",
                                    "description": "exact label text next to the value to "
                                    "extract (e.g. 'Savings') — NOT the value",
                                },
                            },
                            "required": ["name", "anchor_text"],
                        },
                    },
                },
                "required": ["summary", "checkpoint_heading", "identity_anchor_text", "outputs"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_outcome",
            "description": "The application answered with a legitimate business outcome "
            "(e.g. record not found). This is an ANSWER, not a failure.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "one of the declared outcome codes",
                    },
                    "marker_text": {
                        "type": "string",
                        "description": "a distinctive phrase from the outcome message that "
                        "does NOT include any specific parameter value",
                    },
                    "region_anchor_text": {
                        "type": "string",
                        "description": "exact heading/label text of the area with the message",
                    },
                },
                "required": ["code", "marker_text", "region_anchor_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stuck",
            "description": "You cannot safely proceed. Explain why.",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
    },
]

_SYSTEM_PROMPT = """You operate a real web application to accomplish a goal, one action at a time.

Rules:
- Call exactly one tool per turn.
- Only act on refs listed in the CURRENT observation. Refs change every turn.
- To enter a parameter value, type the placeholder exactly as given (e.g. {param:member_id}); \
the system substitutes the real value. Never invent parameter values.
- If the application answers with a legitimate business outcome you were told to expect \
(e.g. a record does not exist), call report_outcome — that is a correct ending.
- When the goal state is on screen, call done with the exact heading text you can see, the \
label next to the identifying value, and the label next to each requested output value.
- Anchors and markers must be exact visible text, and must never contain the specific \
parameter values of this run.
- If you cannot proceed safely, call stuck. Never guess."""


@dataclass
class PlannedStep:
    id: str
    intent: str
    action: dict[str, Any]
    target: dict[str, Any]
    pre: list[PreCondition]
    post: list[PostCondition]
    risk: Literal["safe", "risky"]
    ladder_notes: list[str]


@dataclass
class DoneCall:
    summary: str
    checkpoint_heading: str
    identity_anchor_text: str
    outputs: list[dict[str, str]]


@dataclass
class OutcomeCall:
    code: str
    marker_text: str
    region_anchor_text: str


@dataclass
class PlannerResult:
    ending: Literal["done", "outcome", "stuck", "exhausted"]
    steps: list[PlannedStep]
    done: DoneCall | None = None
    outcome: OutcomeCall | None = None
    stuck_reason: str | None = None
    final_observation: Observation | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    invalid_proposals: int = 0
    llm_calls: int = 0


@dataclass
class Planner:
    model: PlannerModel
    max_steps: int = 12
    max_invalid: int = 6

    _pending_post: PlannedStep | None = field(default=None, init=False)

    def run(
        self,
        surface: WebSurface,
        trace: Trace,
        goal: str,
        params: dict[str, str],
        param_notes: str,
        outcome_codes: dict[str, str],
    ) -> PlannerResult:
        result = PlannerResult(ending="exhausted", steps=[])
        outcomes_text = (
            "\n".join(f"- {code}: {desc}" for code, desc in outcome_codes.items()) or "(none)"
        )
        messages: list[Message] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"GOAL: {goal}\n\nPARAMETERS (use placeholders, never invent values):\n"
                    f"{param_notes}\n\nEXPECTED BUSINESS OUTCOMES you may report:\n{outcomes_text}"
                ),
            },
        ]
        observation = build_observation(surface)
        headings_before = observation.headings(surface)
        url_before = surface.page.url
        messages.append({"role": "user", "content": observation.render()})

        while result.llm_calls < self.max_steps + self.max_invalid:
            turn = self.model.complete(messages, TOOLS)
            result.llm_calls += 1
            result.prompt_tokens += turn.prompt_tokens
            result.completion_tokens += turn.completion_tokens
            trace.emit(
                "llm_call",
                prompt_tokens=turn.prompt_tokens,
                completion_tokens=turn.completion_tokens,
            )
            messages.append(turn.assistant_message)

            call = turn.tool_calls[0] if turn.tool_calls else None
            if call is None or call.parse_error is not None:
                result.invalid_proposals += 1
                trace.emit("planner_invalid", reason="no tool call or bad JSON")
                messages.append(
                    {
                        "role": "user",
                        "content": "You must respond with exactly one valid tool call.",
                    }
                )
                if result.invalid_proposals >= self.max_invalid:
                    result.ending = "stuck"
                    result.stuck_reason = "too many invalid proposals"
                    return result
                continue

            if call.name == "act":
                error = self._try_act(surface, trace, call, params, observation, result)
                # Whatever happened, re-observe and settle the pending step's
                # postconditions against the new state.
                observation = build_observation(surface)
                self._settle_pending(
                    surface, observation, headings_before, url_before, params, result
                )
                headings_before = observation.headings(surface)
                url_before = surface.page.url
                if error is not None:
                    result.invalid_proposals += 1
                    if result.invalid_proposals >= self.max_invalid:
                        result.ending = "stuck"
                        result.stuck_reason = f"too many invalid proposals; last: {error}"
                        return result
                _append_tool_result(messages, call, error or "ok")
                messages.append({"role": "user", "content": observation.render()})
                if len(result.steps) >= self.max_steps:
                    result.ending = "exhausted"
                    result.final_observation = observation
                    return result
                continue

            if call.name == "done":
                result.ending = "done"
                result.done = DoneCall(
                    summary=str(call.arguments.get("summary", "")),
                    checkpoint_heading=str(call.arguments.get("checkpoint_heading", "")),
                    identity_anchor_text=str(call.arguments.get("identity_anchor_text", "")),
                    outputs=[
                        {
                            "name": str(o.get("name", "")),
                            "anchor_text": str(o.get("anchor_text", "")),
                        }
                        for o in call.arguments.get("outputs", [])
                        if isinstance(o, dict)
                    ],
                )
                result.final_observation = observation
                trace.emit("planner_done", summary=result.done.summary)
                return result

            if call.name == "report_outcome":
                code = str(call.arguments.get("code", ""))
                if code not in outcome_codes:
                    result.invalid_proposals += 1
                    _append_tool_result(
                        messages,
                        call,
                        f"unknown outcome code {code!r}; declared: {sorted(outcome_codes)}",
                    )
                    continue
                result.ending = "outcome"
                result.outcome = OutcomeCall(
                    code=code,
                    marker_text=str(call.arguments.get("marker_text", "")),
                    region_anchor_text=str(call.arguments.get("region_anchor_text", "")),
                )
                result.final_observation = observation
                trace.emit("planner_outcome", code=code)
                return result

            # stuck (or an unknown tool name, which counts as stuck-by-confusion)
            result.ending = "stuck"
            result.stuck_reason = str(call.arguments.get("reason", f"unknown tool {call.name!r}"))
            result.final_observation = observation
            trace.emit("planner_stuck", reason=result.stuck_reason)
            return result

        result.ending = "exhausted"
        return result

    # ------------------------------------------------------------------ act

    def _try_act(
        self,
        surface: WebSurface,
        trace: Trace,
        call: ToolCall,
        params: dict[str, str],
        observation: Observation,
        result: PlannerResult,
    ) -> str | None:
        """Validate and execute one proposed action. Returns an error string
        for the model (None on success)."""
        kind = call.arguments.get("kind")
        ref = str(call.arguments.get("ref", ""))
        intent = str(call.arguments.get("intent", "")) or f"{kind} {ref}"
        node = observation.nodes.get(ref)
        if kind not in ("click", "type"):
            return f"invalid kind {kind!r}"
        if node is None:
            return f"ref {ref!r} is not in the current observation"
        text = str(call.arguments.get("text", ""))
        if kind == "type":
            if not text:
                return "type requires text"
            for name in placeholders_in(text):
                if name not in params:
                    return f"unknown parameter {name!r} in text"

        try:
            ladder, notes = build_ladder(surface, node)
        except DistillationError as exc:
            return f"cannot record a reliable locator for {ref}: {exc}"

        # Non-GET requests observed during the action mark the step risky —
        # the mutating-by-default posture. Signals only ever RAISE risk.
        non_get_seen = False

        def on_request(request: Any) -> None:
            nonlocal non_get_seen
            if request.method != "GET":
                non_get_seen = True

        surface.page.on("request", on_request)
        try:
            if kind == "type":
                node.locator.fill(resolve_text(text, params), timeout=5000)
            else:
                node.locator.click(timeout=5000)
        except PlaywrightError as exc:
            return f"action failed on the live page: {exc}"
        finally:
            surface.page.remove_listener("request", on_request)

        step_id = f"s{len(result.steps) + 1}"
        action: dict[str, Any] = (
            {"kind": "type", "text": text} if kind == "type" else {"kind": "click"}
        )
        planned = PlannedStep(
            id=step_id,
            intent=intent,
            action=action,
            target=ladder.model_dump(by_alias=True),
            pre=[PreCondition(kind="editable" if kind == "type" else "visible")],
            post=[],  # settled after the next observation
            risk="risky" if non_get_seen else "safe",
            ladder_notes=notes,
        )
        result.steps.append(planned)
        self._pending_post = planned
        trace.emit(
            "planner_acted",
            step=step_id,
            kind=kind,
            intent=intent,
            risk=planned.risk,
            ladder=[r.strategy for r in ladder.ladder],
            notes=notes,
        )
        return None

    def _settle_pending(
        self,
        surface: WebSurface,
        observation: Observation,
        headings_before: set[str],
        url_before: str,
        params: dict[str, str],
        result: PlannerResult,
    ) -> None:
        """Derive the pending step's postconditions from what actually changed.

        Preference order is data-independent page chrome: a newly appeared
        heading, then a URL change (parameter values scrubbed), then — for
        type actions with a placeholder — the typed value itself. A step must
        end with at least one postcondition; the weakest honest signal wins
        over an invented strong one.
        """
        step = self._pending_post
        if step is None:
            return
        self._pending_post = None
        post: list[PostCondition] = []
        new_headings = observation.headings(surface) - headings_before
        action_kind = step.action.get("kind")
        if action_kind == "type":
            text = str(step.action.get("text", ""))
            names = placeholders_in(text)
            if len(names) == 1 and text == f"{{param:{names[0]}}}":
                post.append(ValueMatchesParam(param=names[0], normalize="none"))
        if new_headings and action_kind == "click":
            heading = sorted(new_headings)[0]
            context = step.target.get("context", []) if isinstance(step.target, dict) else []
            post.append(
                RoleNameVisible.model_validate(
                    {
                        "kind": "role_name_visible",
                        "role": "heading",
                        "name": heading,
                        "context": context,
                    }
                )
            )
        if not post:
            url_now = surface.page.url
            if url_now != url_before:
                from urllib.parse import urlparse

                path = urlparse(url_now).path
                post.append(UrlMatches(pattern=scrub_url_pattern(path, params)))
        if not post:
            # Nothing observable changed: record the weakest true statement.
            from urllib.parse import urlparse

            post.append(
                UrlMatches(pattern=scrub_url_pattern(urlparse(surface.page.url).path, params))
            )
        step.post = post


def _append_tool_result(messages: list[Message], call: ToolCall, content: str) -> None:
    messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
