"""Discovery end-to-end, offline: a scripted model drives the planner loop
through the real browser, the recorder distills a real artifact, and the
slice-1 replay engine executes it with fresh parameters.

This proves the pipeline's mechanics with zero network and zero provider
dependency; the live-model run is evidence, this is the test.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest

from hands.artifact import load_capability
from hands.discover import DiscoveryRequest, discover, load_request
from hands.llm import LlmTurn, Message, ToolCall, ToolSpec
from hands.replay import EngineConfig, ReplayEngine
from hands.results import BusinessOutcome, Success
from tests.conftest import REPO_ROOT

REQUEST_PATH = REPO_ROOT / "capabilities" / "requests" / "lookup_member_balance.request.json"

_TEXTBOX_RE = re.compile(r"\[(e\d+)\] textbox\b(?![^\n]*value=)[^\n]*nearby_label='Member ID'")
_FILLED_RE = re.compile(r"\[(e\d+)\] textbox\b[^\n]*value='\d+'")
_BUTTON_RE = re.compile(r"\[(e\d+)\] button name='Search'")


@dataclass
class ScriptedModel:
    """Behaves like a competent planner model, driven by the observation text.

    ``prelude`` injects misbehaviors first (a reply with no tool call, an act
    on a nonexistent ref) so the planner's corrective loop is exercised.
    """

    prelude: list[str] = field(default_factory=list)
    corrections_seen: int = 0
    _counter: int = 0

    def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LlmTurn:
        del tools
        # A correction may be followed by a fresh observation message, so scan
        # the tail rather than only the last message.
        for message in messages[-3:]:
            content = str(message.get("content", ""))
            if message.get("role") in ("user", "tool") and (
                "must respond" in content or "not in the current observation" in content
            ):
                self.corrections_seen += 1
                break

        if self.prelude:
            misbehavior = self.prelude.pop(0)
            if misbehavior == "no_tool_call":
                return LlmTurn(
                    assistant_message={"role": "assistant", "content": "I think I should search."},
                    tool_calls=[],
                    prompt_tokens=42,
                    completion_tokens=7,
                )
            if misbehavior == "bad_ref":
                return self._turn("act", {"kind": "click", "ref": "e999", "intent": "click ghost"})
            if misbehavior == "bad_json":
                self._counter += 1
                call_id = f"call_{self._counter}"
                return LlmTurn(
                    assistant_message={
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {"name": "act", "arguments": '{"kind": "cli'},
                            }
                        ],
                    },
                    tool_calls=[
                        ToolCall(
                            id=call_id, name="act", arguments={}, parse_error="truncated JSON"
                        )
                    ],
                    prompt_tokens=42,
                    completion_tokens=7,
                )
            if misbehavior == "two_calls":
                first = self._turn("act", {"kind": "click", "ref": "e1", "intent": "a"})
                second = self._turn("act", {"kind": "click", "ref": "e2", "intent": "b"})
                calls = first.tool_calls + second.tool_calls
                return LlmTurn(
                    assistant_message={
                        "role": "assistant",
                        "tool_calls": (
                            first.assistant_message["tool_calls"]
                            + second.assistant_message["tool_calls"]
                        ),
                    },
                    tool_calls=calls,
                    prompt_tokens=42,
                    completion_tokens=7,
                )
            if misbehavior == "literal_value":
                box = _TEXTBOX_RE.search(self._latest_observation(messages))
                ref = box.group(1) if box else "e1"
                return self._turn(
                    "act",
                    {"kind": "type", "ref": ref, "text": "12345", "intent": "type literal"},
                )

        goal = str(messages[1].get("content", ""))
        outcome_mode = "expected to produce the business outcome" in goal
        observation = self._latest_observation(messages)

        if outcome_mode and "No member matches" in observation:
            return self._turn(
                "report_outcome",
                {
                    "code": "MEMBER_NOT_FOUND",
                    "marker_text": "No member matches",
                    "region_anchor_text": "Search Results",
                },
            )
        if not outcome_mode and "Member Details" in observation:
            return self._turn(
                "done",
                {
                    "summary": "Found the member and their savings balance.",
                    "checkpoint_heading": "Member Details",
                    "identity_anchor_text": "Member #",
                    "outputs": [{"name": "savings_balance", "anchor_text": "Savings"}],
                },
            )
        empty_box = _TEXTBOX_RE.search(observation)
        if empty_box:
            return self._turn(
                "act",
                {
                    "kind": "type",
                    "ref": empty_box.group(1),
                    "text": "{param:member_id}",
                    "intent": "Enter the member ID in the search field",
                },
            )
        button = _BUTTON_RE.search(observation)
        if button and _FILLED_RE.search(observation):
            return self._turn(
                "act",
                {"kind": "click", "ref": button.group(1), "intent": "Submit the member search"},
            )
        return self._turn("stuck", {"reason": "scripted model saw an unexpected page"})

    def _turn(self, name: str, args: dict[str, object]) -> LlmTurn:
        self._counter += 1
        call_id = f"call_{self._counter}"
        return LlmTurn(
            assistant_message={
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ],
            },
            tool_calls=[ToolCall(id=call_id, name=name, arguments=dict(args))],
            prompt_tokens=100,
            completion_tokens=20,
        )

    @staticmethod
    def _latest_observation(messages: list[Message]) -> str:
        for message in reversed(messages):
            content = str(message.get("content", ""))
            if message.get("role") == "user" and "actionable elements" in content:
                return content
        return ""


@pytest.fixture()
def request_for(fixture_app: str) -> DiscoveryRequest:
    request = load_request(REQUEST_PATH)
    request.target.entry.web.url = fixture_app + "/"
    return request


def test_discovery_produces_an_artifact_replay_can_execute(
    request_for: DiscoveryRequest, tmp_path: Path
) -> None:
    report = discover(
        request_for,
        ScriptedModel(),
        runs_dir=tmp_path / "runs",
        out_dir=tmp_path / "generated",
    )
    assert report.artifact_path is not None
    assert report.happy.ending == "done"
    assert report.outcome_runs["MEMBER_NOT_FOUND"].ending == "outcome"

    capability = load_capability(report.artifact_path)
    assert capability.conditions[0].provenance == "discovered"
    # The unlabeled legacy input cannot carry a role+name rung; the proximity
    # rung must have survived round-trip verification.
    strategies = [r.strategy for r in capability.steps[0].target.ladder]  # type: ignore[union-attr]
    assert "relative" in strategies
    # The search submit POSTs: the mutating-by-default rule marks it risky.
    assert capability.steps[-1].risk == "risky"

    # The real test: REPLAY the discovered artifact with FRESH parameters.
    engine = ReplayEngine(EngineConfig(runs_dir=tmp_path / "replay-runs"))
    result = engine.run(capability, {"member_id": "67890"})
    assert isinstance(result, Success)
    assert result.outputs == {"savings_balance": Decimal("52.00")}

    missing = engine.run(capability, {"member_id": "99999"})
    assert isinstance(missing, BusinessOutcome)
    assert missing.code == "MEMBER_NOT_FOUND"


def _assert_tool_protocol(transcript_path: Path) -> None:
    """Every assistant message carrying tool_calls must be immediately
    followed by tool messages answering every id — the rule OpenAI-compatible
    providers enforce with a 400. The corrective paths must never violate it."""
    messages = json.loads(transcript_path.read_text())
    for i, message in enumerate(messages):
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        if not calls:
            continue
        expected_ids = {c["id"] for c in calls}
        j = i + 1
        while j < len(messages) and messages[j].get("role") == "tool":
            expected_ids.discard(messages[j].get("tool_call_id"))
            j += 1
        assert not expected_ids, f"unanswered tool_call ids at message {i}: {expected_ids}"


def test_planner_corrects_model_misbehavior(
    request_for: DiscoveryRequest, tmp_path: Path
) -> None:
    """No-tool-call replies, ghost refs, malformed JSON arguments, parallel
    tool calls, and literal parameter values are all rejected with corrective
    feedback ON THE RIGHT CHANNEL — and discovery still completes."""
    model = ScriptedModel(
        prelude=["no_tool_call", "bad_ref", "bad_json", "two_calls", "literal_value"]
    )
    report = discover(
        request_for,
        model,
        runs_dir=tmp_path / "runs",
        out_dir=tmp_path / "generated",
    )
    assert report.happy.ending == "done"
    assert report.happy.invalid_proposals == 5

    # The transcript must be protocol-clean despite all the misbehavior:
    # a protocol violation here would have 400'd a real provider mid-run.
    happy_dir = next(d for d in (tmp_path / "runs").iterdir() if "member_not_found" not in d.name)
    _assert_tool_protocol(happy_dir / "transcript.json")


def test_generated_artifact_marks_search_submit_risky(
    request_for: DiscoveryRequest, tmp_path: Path
) -> None:
    """The extended risk window must catch the form POST that fires while the
    page reacts — mutating-by-default is a property, not a race."""
    report = discover(
        request_for,
        ScriptedModel(),
        runs_dir=tmp_path / "runs",
        out_dir=tmp_path / "generated",
    )
    assert report.artifact_path is not None
    capability = load_capability(report.artifact_path)
    assert capability.steps[-1].risk == "risky"
