"""The web surface: how the system perceives and acts on a browser page.

This is the seam between "the recorded flow" and "how we act on a surface".
Everything above this module speaks in artifact terms (ladders, context paths,
conditions); everything Playwright-specific lives here.

Ladder semantics (pinned, part of the schema contract):
- matching is exact, whitespace-normalized;
- uniqueness is counted over VISIBLE elements only;
- 0 matches -> try the next rung; >1 matches -> hard failure. Lower rungs are
  fallbacks, not disambiguators — descending on ambiguity would be guessing.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import (
    Browser,
    Frame,
    Locator,
    Page,
    Playwright,
    Route,
    sync_playwright,
)
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Request as PwRequest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from hands.artifact import (
    AnchorRung,
    ContextSegment,
    CssRung,
    Fingerprint,
    LabelRung,
    RelativeRung,
    RoleRung,
    Rung,
    TargetLadder,
    TextRung,
)

__all__ = [
    "FingerprintMismatch",
    "PlaywrightError",
    "PlaywrightTimeoutError",
    "ResolvedTarget",
    "SurfaceError",
    "TargetAmbiguous",
    "TargetNotFound",
    "WebSurface",
    "describe_rung",
]


class SurfaceError(Exception):
    """Base class for surface-level resolution errors."""


class TargetNotFound(SurfaceError):
    """Every rung of a ladder produced zero visible matches."""

    def __init__(self, tried: list[str]) -> None:
        self.tried = tried
        super().__init__(f"no rung matched; tried: {'; '.join(tried)}")


class TargetAmbiguous(SurfaceError):
    """A rung matched more than one visible element. Never guess."""

    def __init__(self, rung: str, count: int) -> None:
        self.rung = rung
        self.count = count
        super().__init__(f"{rung} matched {count} visible elements; refusing to choose")


class FingerprintMismatch(SurfaceError):
    def __init__(self, detail: str) -> None:
        super().__init__(f"resolved element does not match recorded fingerprint: {detail}")


@dataclass
class ResolvedTarget:
    locator: Locator
    rung_index: int
    rung: str


def describe_rung(rung: Rung) -> str:
    if isinstance(rung, RoleRung):
        return f"role={rung.role} name={rung.name!r}"
    if isinstance(rung, LabelRung):
        return f"label={rung.text!r}"
    if isinstance(rung, TextRung):
        return f"text={rung.text!r}"
    if isinstance(rung, RelativeRung):
        return f"relative:{rung.relation} of ({describe_rung(rung.anchor)})"
    return f"css={rung.css!r}"


@dataclass
class _Box:
    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height


def _box_of(locator: Locator) -> _Box | None:
    box = locator.bounding_box()
    if box is None:
        return None
    return _Box(x=box["x"], y=box["y"], width=box["width"], height=box["height"])


class WebSurface:
    """Owns the browser session. All Playwright interaction funnels through
    here — including during human handoff, when this same session is what the
    operator drives (a later slice)."""

    def __init__(self, headed: bool = False, attempt_timeout_ms: int = 2000) -> None:
        self._headed = headed
        self.attempt_timeout_ms = attempt_timeout_ms
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._page: Page | None = None

    # ------------------------------------------------------------- lifecycle

    # Injected once at context creation (listeners injected at takeover time
    # would die on the first navigation). Reports ONLY semantic descriptors —
    # never field values: on legacy surfaces SSNs live in plain text inputs
    # and no heuristic can find them all, so values are categorically
    # excluded and only a masked length is reported for typing.
    _CAPTURE_JS = """
    (() => {
      const describe = (el) => ({
        tag: el.tagName ? el.tagName.toLowerCase() : '',
        text: ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName)
          ? '' : (el.innerText || el.value || '').trim().slice(0, 80),
      });
      document.addEventListener('click', (e) => {
        if (window.__handsHumanEvent) {
          window.__handsHumanEvent({ kind: 'click', ...describe(e.target) });
        }
      }, true);
      document.addEventListener('input', (e) => {
        if (window.__handsHumanEvent) {
          const value = e.target && e.target.value ? e.target.value : '';
          window.__handsHumanEvent({
            kind: 'input',
            tag: e.target.tagName ? e.target.tagName.toLowerCase() : '',
            masked_length: value.length,
          });
        }
      }, true);
    })();
    """

    def start(
        self,
        entry_url: str,
        on_human_event: Callable[[dict[str, object]], None] | None = None,
        allowed_hosts: list[str] | None = None,
    ) -> None:
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=not self._headed)
        self._page = self._browser.new_page()
        self.blocked_requests: list[str] = []
        if allowed_hosts is not None:
            allowed = set(allowed_hosts)

            def enforce(route: Route, request: PwRequest) -> None:
                host = urlparse(request.url).netloc
                if host in allowed:
                    route.continue_()
                else:
                    self.blocked_requests.append(request.url)
                    route.abort()

            # Network-layer enforcement: context-wide routing covers every
            # request — clicks, redirects, popups, iframes, subresources —
            # not just navigations the engine initiates itself.
            self._page.route("**/*", enforce)
        if on_human_event is not None:
            self._page.expose_binding(
                "__handsHumanEvent",
                lambda _source, event: on_human_event(dict(event)),
            )
            self._page.add_init_script(self._CAPTURE_JS)
        self._page.goto(entry_url)

    def stop(self) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._browser = None
        self._playwright = None
        self._page = None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise SurfaceError("surface not started")
        return self._page

    # ------------------------------------------------------------- context

    def frame_for(self, context: list[ContextSegment]) -> Frame:
        """Resolve a context path to a frame, re-walked from the page root on
        every call — frame handles are never cached across polls, because
        legacy pages reload frames on navigation."""
        frame = self.page.main_frame
        for segment in context:
            frame = self._child_frame(frame, segment)
        return frame

    @staticmethod
    def _child_frame(parent: Frame, segment: ContextSegment) -> Frame:
        children = parent.child_frames
        if segment.name is not None:
            for child in children:
                if child.name == segment.name:
                    return child
        if segment.url_pattern is not None:
            for child in children:
                if re.search(segment.url_pattern, child.url):
                    return child
        if segment.ordinal is not None and 0 <= segment.ordinal < len(children):
            return children[segment.ordinal]
        raise SurfaceError(f"no child frame matches context segment {segment!r}")

    # ------------------------------------------------------------- resolution

    def resolve(self, target: TargetLadder, within: Locator | None = None) -> ResolvedTarget:
        """Walk the ladder: first rung with exactly one visible match wins.
        Zero everywhere -> TargetNotFound. More than one -> TargetAmbiguous.

        ``within`` scopes resolution to a container (a resolved region), so
        extraction can be anchored inside an identity-verified area.
        """
        frame = self.frame_for(target.context)
        base: Frame | Locator = within if within is not None else frame
        tried: list[str] = []
        for index, rung in enumerate(target.ladder):
            label = describe_rung(rung)
            matches = self._rung_matches(base, rung)
            if len(matches) == 0:
                tried.append(f"{label} (0 matches)")
                continue
            if len(matches) > 1:
                raise TargetAmbiguous(label, len(matches))
            resolved = ResolvedTarget(locator=matches[0], rung_index=index, rung=label)
            if target.fingerprint is not None:
                self._verify_fingerprint(frame, resolved, target.fingerprint)
            return resolved
        raise TargetNotFound(tried)

    def resolve_region(self, target: TargetLadder) -> Locator:
        return self.resolve(target).locator

    def _rung_matches(self, base: Frame | Locator, rung: Rung) -> list[Locator]:
        if isinstance(rung, RoleRung):
            return _visible(base.get_by_role(rung.role, name=rung.name, exact=True))  # type: ignore[arg-type]
        if isinstance(rung, LabelRung):
            return _visible(base.get_by_label(rung.text, exact=True))
        if isinstance(rung, TextRung):
            return _innermost(_visible(base.get_by_text(rung.text, exact=True)))
        if isinstance(rung, CssRung):
            return _visible(base.locator(rung.css))
        return self._relative_matches(base, rung)

    def _anchor(self, base: Frame | Locator, anchor: AnchorRung) -> Locator | None:
        """Resolve a relative rung's anchor. Zero matches -> the rung yields
        nothing (ladder continues); ambiguity -> refuse (never guess)."""
        matches = self._rung_matches(base, anchor)
        if len(matches) == 0:
            return None
        if len(matches) > 1:
            raise TargetAmbiguous(f"anchor {describe_rung(anchor)}", len(matches))
        return matches[0]

    def _relative_matches(self, base: Frame | Locator, rung: RelativeRung) -> list[Locator]:
        anchor = self._anchor(base, rung.anchor)
        if anchor is None:
            return []
        if rung.relation == "container_of":
            # The enclosing group containing the anchor. On the web surface
            # that is the nearest ancestor table; a desktop surface would map
            # this to the enclosing group/pane.
            container = anchor.locator("xpath=ancestor::table[1]")
            return _visible(container)

        anchor_box = _box_of(anchor)
        if anchor_box is None:
            return []
        if rung.relation == "cell_right":
            candidates = _visible(base.locator("td, th"))
        else:  # nearest_input_right
            candidates = _visible(base.locator("input, select, textarea"))

        scored: list[tuple[float, Locator]] = []
        for candidate in candidates:
            box = _box_of(candidate)
            if box is None:
                continue
            # Same visual row means the boxes actually OVERLAP vertically —
            # a center-distance tolerance would admit the adjacent row (e.g.
            # a table's header cell in the same column).
            overlap = min(anchor_box.bottom, box.bottom) - max(anchor_box.y, box.y)
            same_row = overlap >= 0.5 * min(anchor_box.height, box.height)
            to_the_right = box.x >= anchor_box.right - 1.0
            if not (same_row and to_the_right):
                continue
            scored.append((box.x - anchor_box.right, candidate))
        if not scored:
            return []
        # A geometric tie is ambiguity. Returning the DOM-order winner would
        # smuggle a guess past the exactly-one rule (a rowspan anchor sees one
        # equally-near cell per spanned row); return ALL tied candidates and
        # let resolve() refuse.
        best = min(distance for distance, _ in scored)
        return [candidate for distance, candidate in scored if distance - best <= 0.75]

    def _verify_fingerprint(
        self, frame: Frame, resolved: ResolvedTarget, fingerprint: Fingerprint
    ) -> None:
        """A unique match is not necessarily the right match. Verify the
        element against expectations recorded when the flow was learned, using
        the same role engine the ladder resolves with."""
        if fingerprint.role is not None:
            role_matches = resolved.locator.and_(frame.get_by_role(fingerprint.role))  # type: ignore[arg-type]
            if role_matches.count() != 1:
                raise FingerprintMismatch(
                    f"expected role {fingerprint.role!r} (via {resolved.rung})"
                )
        if fingerprint.editable is not None:
            actual = resolved.locator.is_editable()
            if actual != fingerprint.editable:
                raise FingerprintMismatch(f"expected editable={fingerprint.editable}, got {actual}")

    # ------------------------------------------------------------- acting

    def type_text(self, resolved: ResolvedTarget, text: str) -> None:
        resolved.locator.fill(text, timeout=self.attempt_timeout_ms)

    def click(self, resolved: ResolvedTarget) -> None:
        resolved.locator.click(timeout=self.attempt_timeout_ms)

    def goto(self, url: str) -> None:
        self.page.goto(url)

    # ------------------------------------------------------------- evidence

    def capture_evidence(self, run_dir: Path) -> tuple[str | None, str | None]:
        """Screenshot + per-frame accessibility snapshot. Best-effort: evidence
        capture must never turn a reportable failure into a crash."""
        screenshot_path: str | None = None
        snapshot_path: str | None = None
        try:
            path = run_dir / "failure.png"
            self.page.screenshot(path=str(path), full_page=True)
            screenshot_path = str(path)
        except PlaywrightError:
            pass
        try:
            parts: list[str] = []
            for frame in self.page.frames:
                try:
                    snapshot = frame.locator("body").aria_snapshot(timeout=1000)
                except PlaywrightError:
                    continue
                parts.append(f"# frame: {frame.name or frame.url}\n{snapshot}")
            path = run_dir / "failure-a11y.txt"
            path.write_text("\n\n".join(parts), encoding="utf-8")
            snapshot_path = str(path)
        except PlaywrightError:
            pass
        return screenshot_path, snapshot_path


def _visible(locator: Locator) -> list[Locator]:
    """Materialize a locator's VISIBLE matches. Text and CSS strategies match
    hidden elements (collapsed panels, duplicated mobile/desktop markup);
    counting those would make the uniqueness rule meaningless."""
    matches: list[Locator] = []
    try:
        count = locator.count()
    except PlaywrightError:
        return []
    for i in range(count):
        nth = locator.nth(i)
        try:
            if nth.is_visible():
                matches.append(nth)
        except PlaywrightError:
            continue  # element went stale mid-iteration: state is changing
    return matches


def _innermost(matches: list[Locator]) -> list[Locator]:
    """Exact-text matching can match an element and its ancestor when text
    nests; keep only elements that do not contain another match."""
    if len(matches) <= 1:
        return matches
    keep: list[Locator] = []
    for i, candidate in enumerate(matches):
        contains_other = False
        for j, other in enumerate(matches):
            if i == j:
                continue
            try:
                is_ancestor = candidate.evaluate(
                    "(node, other) => node !== other && node.contains(other)",
                    other.element_handle(),
                )
            except PlaywrightError:
                continue
            if is_ancestor:
                contains_other = True
                break
        if not contains_other:
            keep.append(candidate)
    return keep
