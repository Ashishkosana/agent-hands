"""Condition evaluation: pre/postconditions, requires, checkpoints, recognizers.

Everything here is a *sensor*: evaluation never acts on the page. A condition
that cannot be evaluated because the page is mid-transition reports "not
matched right now" — the engine's bounded polling decides what that means.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from playwright.sync_api import Locator

from hands.artifact import (
    Capability,
    Condition,
    PostCondition,
    RegionTextMatch,
    RegionTextMatchesParam,
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
)
from hands.values import normalize


@dataclass
class RecognizerHit:
    condition: Condition
    evidence: MatchEvidence


def describe_state(cond: StateCondition | PostCondition) -> str:
    """Human-readable rendering, used in failure reports and traces."""
    if isinstance(cond, RoleNameVisible):
        return f"a visible {cond.role} named {cond.name!r}"
    if isinstance(cond, TextVisible):
        return f"visible text {cond.text!r}"
    if isinstance(cond, UrlMatches):
        return f"url matching /{cond.pattern}/"
    if isinstance(cond, RegionTextMatchesParam):
        return f"region {cond.region!r} showing the {cond.param!r} value"
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
            matches = frame.get_by_role(cond.role, name=cond.name, exact=True)  # type: ignore[arg-type]
            return _any_visible(matches)
        if isinstance(cond, TextVisible):
            frame = surface.frame_for(cond.context)
            return _any_visible(frame.get_by_text(cond.text, exact=True))
        if isinstance(cond, UrlMatches):
            frame = surface.frame_for(cond.context)
            return re.search(cond.pattern, frame.url) is not None
        # RegionTextMatchesParam — the identity binding check.
        text = _region_text(surface, capability, cond.region)
        return text is not None and params[cond.param] in text
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
        return normalize(observed, cond.normalize) == normalize(params[cond.param], cond.normalize)
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
        if condition.id in suppressed:
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
            matches = frame.get_by_role(
                condition.match.role, name=condition.match.name, exact=True  # type: ignore[arg-type]
            )
            if _any_visible(matches):
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


def _any_visible(matches: Locator) -> bool:
    try:
        count = matches.count()
        return any(matches.nth(i).is_visible() for i in range(count))
    except PlaywrightError:
        return False
