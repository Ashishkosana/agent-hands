"""Condition evaluation: pre/postconditions, requires, checkpoints, recognizers.

Everything here is a *sensor*: evaluation never acts on the page. A condition
that cannot be evaluated because the page is mid-transition reports "not
matched right now" — the engine's bounded polling decides what that means.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from hands.artifact import (
    Capability,
    Condition,
    OptionSelected,
    PostCondition,
    RegionTextMatch,
    RegionTextMatchesParam,
    RoleNameAbsent,
    RoleNameMatch,
    RoleNameVisible,
    StateCondition,
    TextVisible,
    UrlMatches,
    ValueMatchesParam,
)
from hands.results import MatchEvidence
from hands.surface import (
    PlaywrightError,
    ResolvedTarget,
    SurfaceError,
    WebSurface,
    visible_named_role,
    visible_named_text,
)
from hands.values import param_value_matches, resolve_text, value_bound_in


@dataclass
class RecognizerHit:
    condition: Condition
    evidence: MatchEvidence


def describe_state(cond: StateCondition | PostCondition) -> str:
    """Human-readable rendering, used in failure reports and traces."""
    if isinstance(cond, RoleNameVisible):
        return f"a visible {cond.role} named {cond.name!r}"
    if isinstance(cond, RoleNameAbsent):
        return f"no visible {cond.role} named {cond.name!r}"
    if isinstance(cond, TextVisible):
        return f"visible text {cond.text!r}"
    if isinstance(cond, UrlMatches):
        return f"url matching /{cond.pattern}/"
    if isinstance(cond, RegionTextMatchesParam):
        return f"region {cond.region!r} showing the {cond.param!r} value"
    if isinstance(cond, OptionSelected):
        return f"dropdown option matching {cond.option!r} (match={cond.match})"
    return f"target value matches {cond.param!r} (normalize={cond.normalize})"


def state_holds(
    surface: WebSurface,
    cond: StateCondition,
    capability: Capability,
    params: dict[str, str],
) -> bool:
    """Evaluate a state condition. Mid-navigation errors mean 'not yet'."""
    try:
        if isinstance(cond, RoleNameVisible):
            frame = surface.frame_for(cond.context)
            return bool(visible_named_role(frame, cond.role, cond.name))
        if isinstance(cond, RoleNameAbsent):
            frame = surface.frame_for(cond.context)
            return not bool(visible_named_role(frame, cond.role, cond.name))
        if isinstance(cond, TextVisible):
            frame = surface.frame_for(cond.context)
            return bool(visible_named_text(frame, cond.text))
        if isinstance(cond, UrlMatches):
            frame = surface.frame_for(cond.context)
            return re.search(cond.pattern, frame.url) is not None
        # RegionTextMatchesParam — the identity binding check. Boundary-
        # anchored, never bare containment: member "123" must not pass
        # against a page showing member "12345".
        text = _region_text(surface, capability, cond.region)
        return text is not None and value_bound_in(params[cond.param], text)
    except (PlaywrightError, SurfaceError):
        return False


def post_holds(
    surface: WebSurface,
    cond: PostCondition,
    capability: Capability,
    params: dict[str, str],
    target: ResolvedTarget | None,
) -> bool:
    if isinstance(cond, ValueMatchesParam):
        if target is None:
            return False
        try:
            observed = target.locator.input_value(timeout=500)
        except (PlaywrightError, SurfaceError):
            return False
        return param_value_matches(params[cond.param], observed, cond.normalize)
    if isinstance(cond, OptionSelected):
        if target is None:
            return False
        try:
            selected = target.locator.evaluate(
                "el => el.selectedOptions && el.selectedOptions[0]"
                " ? el.selectedOptions[0].label : ''"
            )
        except (PlaywrightError, SurfaceError):
            return False
        wanted = resolve_text(cond.option, params)
        selected = str(selected)
        if cond.match == "label":
            return selected.strip() == wanted.strip()
        return wanted in selected
    return state_holds(surface, cond, capability, params)


def check_recognizers(
    surface: WebSurface,
    capability: Capability,
    armed: list[Condition],
    suppressed: set[str],
) -> RecognizerHit | None:
    """Evaluate armed recognizers against the current page state.

    ``suppressed`` holds ids of conditions that were already matching *before*
    the step's action — a match that predates the action is stale state from a
    previous interaction and must not classify this step's result (freshness
    rule; prevents "No member matches" left over from the last search from
    answering the current one).
    """
    for condition in armed:
        # Freshness suppression exists to prevent stale state from producing
        # a wrong ANSWER; a recoverable condition is a blocking state, and
        # its presence before the action is exactly when it needs handling.
        if condition.classify == "business_outcome" and condition.id in suppressed:
            continue
        evidence = _match(surface, capability, condition)
        if evidence is not None:
            return RecognizerHit(condition=condition, evidence=evidence)
    return None


def matching_now(
    surface: WebSurface, capability: Capability, armed: list[Condition]
) -> set[str]:
    """Ids of armed recognizers matching at this instant (pre-action snapshot)."""
    return {c.id for c in armed if _match(surface, capability, c) is not None}


def _match(
    surface: WebSurface, capability: Capability, condition: Condition
) -> MatchEvidence | None:
    try:
        if isinstance(condition.match, RoleNameMatch):
            frame = surface.frame_for(condition.match.context)
            if visible_named_role(frame, condition.match.role, condition.match.name):
                return MatchEvidence(
                    condition_id=condition.id,
                    role=condition.match.role,
                    matched_text=condition.match.name,
                )
            return None
        # RegionTextMatch: structural scoping — patterns are only meaningful
        # inside their named region. An absent region is simply "no match".
        match: RegionTextMatch = condition.match
        text = _region_text(surface, capability, match.region)
        if text is None:
            return None
        for pattern in match.patterns:
            if pattern in text:
                line = next((ln for ln in text.splitlines() if pattern in ln), pattern)
                return MatchEvidence(
                    condition_id=condition.id,
                    region=match.region,
                    matched_text=line.strip(),
                )
        return None
    except (PlaywrightError, SurfaceError):
        return None


def _region_text(surface: WebSurface, capability: Capability, region: str) -> str | None:
    ladder = capability.regions[region]
    try:
        container = surface.resolve_region(ladder)
        return container.inner_text(timeout=500)
    except (PlaywrightError, SurfaceError):
        return None
