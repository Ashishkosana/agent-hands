"""The capability artifact schema.

A capability is a contract first and a step list second: parameters, outputs,
and the closed set of business outcome codes live at the top level so a calling
agent can invoke it blind. Steps, locator ladders, and condition recognizers
are implementation detail below that line.

Validators enforce the properties that make the artifact safe to replay:
outcome-code closure, checkpoint identity binding, and no secrets persisted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hands.values import Normalize, placeholders_in

SCHEMA_VERSION = 1


class _Model(BaseModel):
    """Base: reject unknown fields so typos fail loudly at load time."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------- targets


class ContextSegment(_Model):
    """One segment of a surface context path (a frame, on the web surface).

    Recorded redundantly — name, URL pattern, ordinal — so replay can match by
    the most stable attribute available. At least one must be set.
    """

    name: str | None = None
    url_pattern: str | None = None
    ordinal: int | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> ContextSegment:
        if self.name is None and self.url_pattern is None and self.ordinal is None:
            raise ValueError("context segment must set at least one of name/url_pattern/ordinal")
        return self


class RoleRung(_Model):
    """Role + accessible name — what a human operator sees. Reliable for
    buttons/links/text-bearing cells; legacy form controls often have an empty
    accessible name, which is why the ladder continues below."""

    strategy: Literal["role"] = "role"
    role: str
    name: str


class LabelRung(_Model):
    """Real label association only (label[for], aria-label). Proximity-based
    labeling is deliberately a separate rung (relative) so telemetry shows
    which mechanism actually carried the step."""

    strategy: Literal["label"] = "label"
    text: str


class TextRung(_Model):
    """Exact visible text; innermost match wins when matches nest."""

    strategy: Literal["text"] = "text"
    text: str


class CssRung(_Model):
    """Recorded CSS path. Web-only, last resort: positional paths on a changed
    DOM resolve uniquely to the WRONG element, so use emits a fragility warning
    and cross-tenant replay disables this rung entirely."""

    strategy: Literal["css"] = "css"
    css: str


AnchorRung = Annotated[RoleRung | LabelRung | TextRung | CssRung, Field(discriminator="strategy")]

Relation = Literal["cell_right", "nearest_input_right", "container_of"]


class RelativeRung(_Model):
    """Geometric relation to an anchor found by another strategy — how you read
    values out of table-soup ("cell right of 'Savings'") and how unlabeled
    legacy inputs are located ("nearest input right of 'Member ID'")."""

    strategy: Literal["relative"] = "relative"
    relation: Relation
    anchor: AnchorRung
    container: Literal["table"] | None = None

    @model_validator(mode="after")
    def _container_required(self) -> RelativeRung:
        if self.relation == "container_of" and self.container is None:
            raise ValueError("container_of requires a container kind")
        return self


Rung = Annotated[
    RoleRung | LabelRung | TextRung | RelativeRung | CssRung,
    Field(discriminator="strategy"),
]


class Fingerprint(_Model):
    """Expectations about the element itself, recorded at distillation and
    verified at replay. Uniqueness is not correctness; the fingerprint is the
    check that a unique match is also the RIGHT match."""

    role: str | None = None
    editable: bool | None = None


class TargetLadder(_Model):
    ladder: list[Rung] = Field(min_length=1)
    context: list[ContextSegment] = Field(default_factory=list)
    fingerprint: Fingerprint | None = None
    # True when only a CSS path survived round-trip verification at record
    # time: the fragility is part of the reviewable contract, not a buried
    # trace note. Replay emits a warning whenever a fragile target is used.
    fragile: bool = False


class WebEntry(_Model):
    url: str


class Entry(_Model):
    """Surface-typed entry descriptor. A desktop surface would add its own
    variant (app id + window title); the artifact stays surface-neutral."""

    web: WebEntry


class TargetApp(_Model):
    app: str
    entry: Entry


# --------------------------------------------------------------------------- conditions


class RoleNameVisible(_Model):
    kind: Literal["role_name_visible"] = "role_name_visible"
    role: str
    name: str
    context: list[ContextSegment] = Field(default_factory=list)


class TextVisible(_Model):
    kind: Literal["text_visible"] = "text_visible"
    text: str
    context: list[ContextSegment] = Field(default_factory=list)


class UrlMatches(_Model):
    kind: Literal["url_matches"] = "url_matches"
    pattern: str
    context: list[ContextSegment] = Field(default_factory=list)


class RoleNameAbsent(_Model):
    """The negative condition recovery needs: proof that a blocking state is
    GONE ("the Session Expired heading is no longer visible") — the only
    postcondition a dismiss/continue action can truthfully claim, since the
    page it lands on depends on where the flow was interrupted."""

    kind: Literal["role_name_absent"] = "role_name_absent"
    role: str
    name: str
    context: list[ContextSegment] = Field(default_factory=list)


class RegionTextMatchesParam(_Model):
    """Identity binding: the named region's text must contain the invocation's
    parameter value. This is what turns "a details page loaded" into "the
    details page for the member you asked about"."""

    kind: Literal["region_text_matches_param"] = "region_text_matches_param"
    region: str
    param: str


StateCondition = Annotated[
    RoleNameVisible | RoleNameAbsent | TextVisible | UrlMatches | RegionTextMatchesParam,
    Field(discriminator="kind"),
]


class ValueMatchesParam(_Model):
    """Postcondition on the step's own target: its value matches the parameter
    after normalization (UIs reformat what you type)."""

    kind: Literal["value_matches_param"] = "value_matches_param"
    param: str
    normalize: Normalize = "none"


class OptionSelected(_Model):
    """Postcondition on a select step: the dropdown's currently-selected option
    label matches (exact `label` or stable `contains` substring). The select
    analogue of value_matches_param — no unverified selects."""

    kind: Literal["option_selected"] = "option_selected"
    option: str
    match: Literal["label", "contains"] = "label"


PostCondition = Annotated[
    ValueMatchesParam
    | OptionSelected
    | RoleNameVisible
    | RoleNameAbsent
    | TextVisible
    | UrlMatches
    | RegionTextMatchesParam,
    Field(discriminator="kind"),
]

PreKind = Literal["visible", "editable"]


class PreCondition(_Model):
    kind: PreKind


# --------------------------------------------------------------------------- recognizers


class RegionTextMatch(_Model):
    """Pattern list matched against a named region's text — structural scoping,
    never bare text anywhere in the page (help copy saying "if no member
    matches..." must not trigger a business outcome)."""

    kind: Literal["region_text"] = "region_text"
    region: str
    patterns: list[str] = Field(min_length=1)


class RoleNameMatch(_Model):
    kind: Literal["role_name"] = "role_name"
    role: str
    name: str
    context: list[ContextSegment] = Field(default_factory=list)


ConditionMatch = Annotated[RegionTextMatch | RoleNameMatch, Field(discriminator="kind")]

Provenance = Literal["discovered", "authored"]

Classification = Literal["business_outcome", "recoverable"]

ResumePoint = Literal["retry_current_step", "restart_from"]


class Condition(_Model):
    """A recognizer for a known runtime state. Armed from the action of step
    ``armed_after`` onward.

    Two classes: a *business outcome* ends the run with an answer (freshness
    rule applies — a match present before the action may not classify it);
    a *recoverable* condition is a blocking state handled in place (presence
    before the action is exactly when it needs handling, so no suppression),
    bounded by ``max_fires_per_run``, with an explicit resume point — plain
    "continue" is wrong whenever recovery navigates."""

    id: str
    armed_after: str
    match: ConditionMatch
    classify: Classification
    outcome_code: str | None = None
    recovery: list[Step] = Field(default_factory=list)
    resume: ResumePoint | None = None
    restart_from: str | None = None
    max_fires_per_run: int = Field(default=2, ge=0)
    provenance: Provenance
    verified_by_eval: bool = False

    @model_validator(mode="after")
    def _class_coherence(self) -> Condition:
        if self.classify == "business_outcome":
            if self.outcome_code is None:
                raise ValueError(f"condition {self.id!r}: business outcome requires outcome_code")
            if self.recovery or self.resume or self.restart_from:
                raise ValueError(f"condition {self.id!r}: an outcome is an answer, not a recovery")
        else:
            if self.outcome_code is not None:
                raise ValueError(f"condition {self.id!r}: recoverable conditions have no outcome")
            if not self.recovery:
                raise ValueError(f"condition {self.id!r}: recoverable requires recovery steps")
            if self.resume is None:
                raise ValueError(f"condition {self.id!r}: recoverable requires a resume point")
            if (self.resume == "restart_from") != (self.restart_from is not None):
                raise ValueError(
                    f"condition {self.id!r}: restart_from must be set iff resume is restart_from"
                )
        return self


# --------------------------------------------------------------------------- steps


class TypeAction(_Model):
    kind: Literal["type"] = "type"
    text: str


class ClickAction(_Model):
    kind: Literal["click"] = "click"


class NavigateAction(_Model):
    kind: Literal["navigate"] = "navigate"
    url: str


class SelectAction(_Model):
    """Choose an option from a native <select> dropdown. `option` is the option
    to pick (may carry a {param} placeholder); `match` says how to match it to
    the visible option labels — exact `label`, or a stable `contains` substring
    (legacy dropdowns often embed volatile text like a live balance in the
    label, so matching the whole label would break run to run)."""

    kind: Literal["select"] = "select"
    option: str
    match: Literal["label", "contains"] = "label"


Action = Annotated[
    TypeAction | ClickAction | NavigateAction | SelectAction,
    Field(discriminator="kind"),
]

Risk = Literal["safe", "risky"]


class Step(_Model):
    id: str
    intent: str
    action: Action
    target: TargetLadder | None = None
    pre: list[PreCondition] = Field(default_factory=list)
    post: list[PostCondition] = Field(min_length=1)  # no unverified actions
    risk: Risk

    @model_validator(mode="after")
    def _target_required(self) -> Step:
        needs_target = self.action.kind in ("type", "click", "select")
        if needs_target and self.target is None:
            raise ValueError(f"step {self.id!r}: {self.action.kind} action requires a target")
        if not needs_target and self.target is not None:
            raise ValueError(f"step {self.id!r}: navigate action takes no target")
        return self


# --------------------------------------------------------------------------- contract


class ParamSpec(_Model):
    type: Literal["string"] = "string"
    description: str
    sensitive: bool = False
    example: str | None = None
    pattern: str | None = None

    @model_validator(mode="after")
    def _sensitive_forbids_example(self) -> ParamSpec:
        if self.sensitive and self.example is not None:
            raise ValueError("a sensitive parameter must not carry an example value")
        return self


class OutcomeSpec(_Model):
    description: str


class MoneyParse(_Model):
    kind: Literal["money"] = "money"
    locale: Literal["en_US"] = "en_US"


class TextParse(_Model):
    kind: Literal["text"] = "text"


ParseSpec = Annotated[MoneyParse | TextParse, Field(discriminator="kind")]


class OutputSpec(_Model):
    type: Literal["decimal", "string"]
    sensitive: bool = False
    region: str | None = None
    source: TargetLadder = Field(alias="from")
    parse: ParseSpec = Field(default_factory=TextParse)

    @model_validator(mode="after")
    def _decimal_needs_parser(self) -> OutputSpec:
        if self.type == "decimal" and self.parse.kind != "money":
            raise ValueError("a decimal output requires a money parse spec")
        return self


class Checkpoint(_Model):
    all: list[StateCondition] = Field(min_length=1)


class RiskReview(_Model):
    reviewed_by: str | None = None
    artifact_hash: str | None = None


class Capability(_Model):
    schema_version: Literal[1]
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    version: int = Field(ge=1)
    description: str
    target: TargetApp
    requires: list[StateCondition] = Field(default_factory=list)
    parameters: dict[str, ParamSpec] = Field(default_factory=dict)
    outcomes: dict[str, OutcomeSpec] = Field(default_factory=dict)
    outputs: dict[str, OutputSpec] = Field(default_factory=dict)
    regions: dict[str, TargetLadder] = Field(default_factory=dict)
    steps: list[Step] = Field(min_length=1)
    conditions: list[Condition] = Field(default_factory=list)
    checkpoint: Checkpoint
    allow_unbound_checkpoint: bool = False
    risk_review: RiskReview = Field(default_factory=RiskReview)

    @model_validator(mode="after")
    def _integrity(self) -> Capability:
        step_ids = [s.id for s in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("step ids must be unique")

        # Outcome closure, both directions: every business-outcome recognizer
        # maps to a declared code, and every declared code is reachable. The
        # caller's contract and the artifact's behavior cannot drift apart.
        declared = set(self.outcomes)
        referenced = {
            c.outcome_code
            for c in self.conditions
            if c.classify == "business_outcome" and c.outcome_code is not None
        }
        if undeclared := referenced - declared:
            raise ValueError(f"conditions reference undeclared outcome codes: {sorted(undeclared)}")
        if unreachable := declared - referenced:
            raise ValueError(f"declared outcomes have no recognizer: {sorted(unreachable)}")

        known_steps = set(step_ids)
        for cond in self.conditions:
            if cond.armed_after not in known_steps:
                raise ValueError(f"condition {cond.id!r}: armed_after references unknown step")
            if cond.restart_from is not None and cond.restart_from not in known_steps:
                raise ValueError(f"condition {cond.id!r}: restart_from references unknown step")
            recovery_ids = [s.id for s in cond.recovery]
            if len(recovery_ids) != len(set(recovery_ids)):
                raise ValueError(f"condition {cond.id!r}: recovery step ids must be unique")

        # Region references must resolve.
        known_regions = set(self.regions)
        for cond in self.conditions:
            if isinstance(cond.match, RegionTextMatch) and cond.match.region not in known_regions:
                raise ValueError(f"condition {cond.id!r}: unknown region {cond.match.region!r}")
        for out_name, out in self.outputs.items():
            if out.region is not None and out.region not in known_regions:
                raise ValueError(f"output {out_name!r}: unknown region {out.region!r}")

        # Placeholder and param-reference validation.
        known_params = set(self.parameters)
        for step in self.steps:
            if step.action.kind == "type":
                for ref in placeholders_in(step.action.text):
                    if ref not in known_params:
                        raise ValueError(
                            f"step {step.id!r}: text references undeclared parameter {ref!r}"
                        )
            elif step.action.kind == "select":
                for ref in placeholders_in(step.action.option):
                    if ref not in known_params:
                        raise ValueError(
                            f"step {step.id!r}: option references undeclared parameter {ref!r}"
                        )
            for post in step.post:
                if isinstance(post, ValueMatchesParam) and post.param not in known_params:
                    raise ValueError(f"step {step.id!r}: undeclared parameter {post.param!r}")

        checkpoint_conditions = list(self.checkpoint.all) + list(self.requires)
        for state in checkpoint_conditions:
            if isinstance(state, RegionTextMatchesParam):
                if state.region not in known_regions:
                    raise ValueError(f"checkpoint/requires: unknown region {state.region!r}")
                if state.param not in known_params:
                    raise ValueError(f"checkpoint/requires: undeclared parameter {state.param!r}")

        # Every declared parameter must be USED — by a typed placeholder, a
        # value postcondition, or an identity binding. An unused parameter is
        # a contract lying about its inputs.
        used: set[str] = set()
        for step in self.steps:
            if step.action.kind == "type":
                used.update(placeholders_in(step.action.text))
            elif step.action.kind == "select":
                used.update(placeholders_in(step.action.option))
            for post in step.post:
                if isinstance(post, ValueMatchesParam):
                    used.add(post.param)
        for state in checkpoint_conditions:
            if isinstance(state, RegionTextMatchesParam):
                used.add(state.param)
        if unused := known_params - used:
            raise ValueError(f"declared parameters are never used: {sorted(unused)}")

        # Checkpoint identity binding: a capability with identifying parameters
        # must prove it reached the record it was asked about, or explicitly
        # waive that proof. "A details page loaded" passing while the previous
        # member is still on screen is the silent-wrong-answer bug.
        if self.parameters and not self.allow_unbound_checkpoint:
            binds = any(isinstance(c, RegionTextMatchesParam) for c in self.checkpoint.all)
            if not binds:
                raise ValueError(
                    "checkpoint references no parameter; a capability with parameters must "
                    "bind identity (or set allow_unbound_checkpoint with justification)"
                )
        return self


def risk_hash(capability: Capability) -> str:
    """Hash of the artifact CONTENT a risk review signs — everything except
    the signature block itself. Any change to steps, ladders, or contract
    invalidates prior approval; re-recording requires re-review."""
    import hashlib

    unsigned = capability.model_copy(update={"risk_review": RiskReview()})
    return hashlib.sha256(dump_capability(unsigned).encode()).hexdigest()


def sign_risk_review(capability: Capability, reviewed_by: str) -> Capability:
    review = RiskReview(reviewed_by=reviewed_by, artifact_hash=risk_hash(capability))
    return capability.model_copy(update={"risk_review": review})


def risk_review_valid(capability: Capability) -> bool:
    review = capability.risk_review
    return review.reviewed_by is not None and review.artifact_hash == risk_hash(capability)


def load_capability(path: Path) -> Capability:
    return Capability.model_validate_json(path.read_text())


def dump_capability(capability: Capability) -> str:
    return capability.model_dump_json(indent=2, by_alias=True) + "\n"
