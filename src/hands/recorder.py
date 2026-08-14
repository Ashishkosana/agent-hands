"""The recorder: distills a live discovery run into a capability artifact.

Two rules make the distillation trustworthy:

1. **Ladders are built at act time and round-tripped immediately.** Every
   candidate rung is resolved through the same locator engine replay uses,
   against the page as it exists at the moment of the action, and is kept
   only if it resolves uniquely to the very element being acted on. A rung
   that cannot round-trip is dropped, not recorded as a latent replay bug.

2. **The model proposes semantics; the recorder verifies mechanically.** The
   planner names a checkpoint heading, an identity anchor, output anchors, and
   outcome markers — the recorder builds the corresponding conditions and
   proves each one against the live page (including the data-independence
   rule: no distilled pattern may contain a parameter value) before it is
   allowed into the artifact.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from playwright.sync_api import Locator

from hands.artifact import (
    Capability,
    Checkpoint,
    Condition,
    ContextSegment,
    CssRung,
    Fingerprint,
    LabelRung,
    OutputSpec,
    RegionTextMatch,
    RegionTextMatchesParam,
    RelativeRung,
    RoleNameVisible,
    RoleRung,
    Rung,
    TargetLadder,
    TextRung,
)
from hands.observe import ActionableNode
from hands.surface import SurfaceError, TargetAmbiguous, WebSurface
from hands.values import value_bound_in

_CSS_PATH_JS = """
el => {
  const parts = [];
  let node = el;
  while (node && node.nodeType === 1 && parts.length < 8) {
    let selector = node.tagName.toLowerCase();
    const parent = node.parentElement;
    if (parent) {
      const siblings = Array.from(parent.children).filter(c => c.tagName === node.tagName);
      if (siblings.length > 1) selector += `:nth-of-type(${siblings.indexOf(node) + 1})`;
    }
    parts.unshift(selector);
    node = parent;
  }
  return parts.join(' > ');
}
"""


class DistillationError(Exception):
    """The run succeeded but could not be distilled into a trustworthy
    artifact — surfaced to a human, never guessed around."""


@dataclass
class RecordedStep:
    """A step captured at act time; postconditions are attached once the
    action's effect has been observed."""

    node_role: str
    ladder: TargetLadder
    ladder_notes: list[str] = field(default_factory=list)


def build_ladder(surface: WebSurface, node: ActionableNode) -> tuple[TargetLadder, list[str]]:
    """Candidate rungs in semantic-first order, each verified by round-trip.

    Returns the ladder plus notes (dropped rungs, css fallback) for the trace.
    """
    candidates: list[Rung] = []
    if node.name:
        candidates.append(RoleRung(role=node.role, name=node.name))
    if node.has_label_association and node.name:
        candidates.append(LabelRung(text=node.name))
    if node.name and node.role in ("button", "link"):
        candidates.append(TextRung(text=node.name))
    if node.editable and node.nearby_text:
        candidates.append(
            RelativeRung(relation="nearest_input_right", anchor=TextRung(text=node.nearby_text))
        )
    css_path = str(node.locator.evaluate(_CSS_PATH_JS))
    if css_path:
        candidates.append(CssRung(css=css_path))

    verified: list[Rung] = []
    notes: list[str] = []
    for rung in candidates:
        if _round_trips(surface, rung, node):
            verified.append(rung)
        else:
            notes.append(f"dropped rung (no unique round-trip): {rung.strategy}")
    if not verified:
        raise DistillationError(
            f"no locator rung round-trips to the acted element (role={node.role}, "
            f"name={node.name!r}, nearby={node.nearby_text!r})"
        )
    fragile = all(isinstance(r, CssRung) for r in verified)
    if fragile:
        notes.append("WARNING: only a CSS path survived verification — fragile target")
    fingerprint = Fingerprint(role=node.role, editable=node.editable if node.editable else None)
    ladder = TargetLadder(
        ladder=verified, context=node.context, fingerprint=fingerprint, fragile=fragile
    )
    return ladder, notes


def _round_trips(surface: WebSurface, rung: Rung, node: ActionableNode) -> bool:
    """A rung is recordable iff it resolves to exactly one visible element AND
    that element is the acted one."""
    try:
        resolved = surface.resolve(
            TargetLadder(ladder=[rung], context=node.context, fingerprint=None)
        )
    except (SurfaceError, TargetAmbiguous):
        return False
    try:
        # Compared against the PINNED handle from observation time, so DOM
        # churn between observation and act cannot swap the target.
        same = bool(resolved.locator.evaluate("(a, b) => a === b", node.handle))
    except Exception:
        return False
    return same


def _reject_param_text(label: str, text: str, params: dict[str, str]) -> None:
    """Data-independence applies to EVERY model-supplied string that becomes
    part of the artifact — anchors, headings, markers. A string carrying this
    run's parameter value would work for this invocation and no other."""
    for name, value in params.items():
        if value and value in text:
            raise DistillationError(
                f"{label} {text!r} contains the value of parameter {name!r}; "
                f"artifact strings must be data-independent"
            )


# ------------------------------------------------------------------ regions


def region_ladder(anchor_text: str, context: list[ContextSegment]) -> TargetLadder:
    return TargetLadder(
        ladder=[
            RelativeRung(
                relation="container_of", container="table", anchor=TextRung(text=anchor_text)
            )
        ],
        context=context,
    )


def verify_region(surface: WebSurface, ladder: TargetLadder) -> Locator:
    try:
        return surface.resolve_region(ladder)
    except SurfaceError as exc:
        raise DistillationError(f"region does not resolve on the live page: {exc}") from exc


# ------------------------------------------------------- finalization checks


def build_identity_checkpoint(
    surface: WebSurface,
    checkpoint_heading: str,
    identity_anchor: str,
    identity_param: str,
    params: dict[str, str],
    context: list[ContextSegment],
) -> tuple[Checkpoint, dict[str, TargetLadder]]:
    """Build the checkpoint the planner proposed and PROVE it on the live
    page: the heading is visible, the identity region resolves, and it
    contains the invocation's parameter value with proper boundaries."""
    _reject_param_text("checkpoint heading", checkpoint_heading, params)
    _reject_param_text("identity anchor", identity_anchor, params)
    regions: dict[str, TargetLadder] = {}
    heading = RoleNameVisible(role="heading", name=checkpoint_heading, context=context)
    # Prove the heading, not just the region: a model-proposed heading that
    # is not actually visible right now must never enter the checkpoint.
    try:
        frame = surface.frame_for(context)
        matches = frame.get_by_role("heading", name=checkpoint_heading, exact=True)
        visible = any(matches.nth(i).is_visible() for i in range(matches.count()))
    except Exception as exc:
        raise DistillationError(f"cannot verify checkpoint heading: {exc}") from exc
    if not visible:
        raise DistillationError(
            f"checkpoint heading {checkpoint_heading!r} is not visible on the live page"
        )
    ladder = region_ladder(identity_anchor, context)
    container = verify_region(surface, ladder)
    text = container.inner_text(timeout=2000)
    if not value_bound_in(params[identity_param], text):
        raise DistillationError(
            f"identity region anchored at {identity_anchor!r} does not contain the "
            f"{identity_param!r} value on the live page — checkpoint would not bind identity"
        )
    regions["identity"] = ladder
    checkpoint = Checkpoint(
        all=[heading, RegionTextMatchesParam(region="identity", param=identity_param)]
    )
    return checkpoint, regions


def build_output(
    surface: WebSurface,
    name: str,
    anchor_text: str,
    spec_type: str,
    sensitive: bool,
    context: list[ContextSegment],
    params: dict[str, str],
    expected_value: str | None,
) -> tuple[OutputSpec, dict[str, TargetLadder]]:
    """Build an output extractor from the planner's anchor and verify it:
    the anchor is data-independent, resolves next to a value on the live
    page, the value parses — and, when the request declares the expected
    value for the example invocation, the extracted value MATCHES it.
    Supervised discovery: a wrong anchor that happens to sit next to money
    must not become a capability that returns wrong balances forever."""
    from hands.values import ValueError_, parse_money

    _reject_param_text(f"output {name!r} anchor", anchor_text, params)
    region_name = f"output_{name}"
    ladder = region_ladder(anchor_text, context)
    container = verify_region(surface, ladder)
    source = TargetLadder(
        ladder=[RelativeRung(relation="cell_right", anchor=TextRung(text=anchor_text))],
        context=context,
    )
    try:
        resolved = surface.resolve(source, within=container)
        raw = resolved.locator.inner_text(timeout=2000).strip()
    except SurfaceError as exc:
        raise DistillationError(
            f"output {name!r} does not resolve next to {anchor_text!r}: {exc}"
        ) from exc
    if spec_type == "decimal":
        try:
            parsed = parse_money(raw)
        except ValueError_ as exc:
            raise DistillationError(
                f"output {name!r} read {'«masked»' if sensitive else raw!r} which does not "
                f"parse as money — wrong anchor or wrong parse spec"
            ) from exc
        if expected_value is not None and parsed != parse_money(expected_value):
            raise DistillationError(
                f"output {name!r} extracted a value that does not match the expected value "
                f"declared for this example invocation — wrong anchor"
            )
    elif expected_value is not None and raw != expected_value:
        raise DistillationError(
            f"output {name!r} extracted a value that does not match the declared expected value"
        )
    output = OutputSpec.model_validate(
        {
            "type": spec_type,
            "sensitive": sensitive,
            "region": region_name,
            "from": source.model_dump(by_alias=True),
            "parse": {"kind": "money", "locale": "en_US"}
            if spec_type == "decimal"
            else {"kind": "text"},
        }
    )
    return output, {region_name: ladder}


def build_outcome_condition(
    surface: WebSurface,
    code: str,
    marker_text: str,
    region_anchor: str,
    armed_after: str,
    params: dict[str, str],
    context: list[ContextSegment],
) -> tuple[Condition, dict[str, TargetLadder]]:
    """Build a business-outcome recognizer from the planner's marker and
    verify it: the region resolves, the marker is present in it right now, and
    — the data-independence rule — the marker contains no parameter value, so
    it will fire for every future invocation, not just this one's."""
    _reject_param_text("outcome marker", marker_text, params)
    _reject_param_text("outcome region anchor", region_anchor, params)
    region_name = f"outcome_{code.lower()}"
    ladder = region_ladder(region_anchor, context)
    container = verify_region(surface, ladder)
    text = container.inner_text(timeout=2000)
    if marker_text not in text:
        raise DistillationError(
            f"outcome marker {marker_text!r} is not present in the region anchored at "
            f"{region_anchor!r} on the live page"
        )
    condition = Condition(
        id=f"cond_{code.lower()}",
        armed_after=armed_after,
        match=RegionTextMatch(region=region_name, patterns=[marker_text]),
        classify="business_outcome",
        outcome_code=code,
        provenance="discovered",
        verified_by_eval=False,
    )
    return condition, {region_name: ladder}


def scrub_url_pattern(url_path: str, params: dict[str, str]) -> str:
    """A URL postcondition must not embed the example run's parameter values —
    /member/12345 becomes /member/[^/]+ (data-independence for URLs)."""
    import re
    from urllib.parse import quote

    pattern = re.escape(url_path)
    values = sorted((v for v in params.values() if v), key=len, reverse=True)
    for value in values:
        for form in {value, quote(value, safe="")}:
            pattern = pattern.replace(re.escape(form), "[^/]+")
    for value in values:
        if value in pattern:
            raise DistillationError(
                f"url pattern still contains a parameter value after scrubbing: {url_path!r}"
            )
    return pattern


def merge_regions(
    into: dict[str, TargetLadder], new: dict[str, TargetLadder], where: str
) -> None:
    for name, ladder in new.items():
        existing = into.get(name)
        if existing is not None and existing != ladder:
            raise DistillationError(
                f"region {name!r} defined twice with different ladders ({where})"
            )
        into[name] = ladder


def assemble(
    *,
    name: str,
    description: str,
    target: dict[str, object],
    requires: list[RoleNameVisible],
    parameters: dict[str, object],
    outcomes: dict[str, object],
    outputs: dict[str, OutputSpec],
    regions: dict[str, TargetLadder],
    steps: list[dict[str, object]],
    conditions: list[Condition],
    checkpoint: Checkpoint,
) -> Capability:
    """Assemble and validate the final artifact. Validation failures here are
    distillation errors: the run happened, but its record is not trustworthy."""
    try:
        return Capability.model_validate(
            {
                "schema_version": 1,
                "name": name,
                "version": 1,
                "description": description,
                "target": target,
                "requires": [r.model_dump() for r in requires],
                "parameters": parameters,
                "outcomes": outcomes,
                "outputs": {k: v.model_dump(by_alias=True) for k, v in outputs.items()},
                "regions": {k: v.model_dump(by_alias=True) for k, v in regions.items()},
                "steps": steps,
                "conditions": [c.model_dump() for c in conditions],
                "checkpoint": checkpoint.model_dump(),
            }
        )
    except ValueError as exc:
        raise DistillationError(f"assembled artifact failed validation: {exc}") from exc
