"""Locator ladder semantics, exercised against controlled markup.

These are the pinned rules of the artifact contract: exact matching,
visible-only counting, 0 matches -> next rung, >1 matches -> refuse.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from hands.artifact import (
    Fingerprint,
    LabelRung,
    RelativeRung,
    RoleRung,
    TargetLadder,
    TextRung,
)
from hands.surface import (
    FingerprintMismatch,
    TargetAmbiguous,
    TargetNotFound,
    WebSurface,
)


@pytest.fixture(scope="module")
def surface() -> Iterator[WebSurface]:
    web = WebSurface(headed=False)
    web.start("about:blank")
    yield web
    web.stop()


def ladder(*rungs: RoleRung | LabelRung | TextRung | RelativeRung) -> TargetLadder:
    return TargetLadder(ladder=list(rungs))


def test_ambiguity_refuses_to_choose(surface: WebSurface) -> None:
    surface.page.set_content(
        "<button>Search</button><button>Search</button>"
    )
    with pytest.raises(TargetAmbiguous):
        surface.resolve(ladder(RoleRung(role="button", name="Search")))


def test_zero_matches_falls_to_next_rung(surface: WebSurface) -> None:
    surface.page.set_content("<button>Go</button>")
    resolved = surface.resolve(
        ladder(LabelRung(text="Nope"), TextRung(text="Go"))
    )
    assert resolved.rung_index == 1


def test_all_rungs_exhausted_is_not_found(surface: WebSurface) -> None:
    surface.page.set_content("<p>empty</p>")
    with pytest.raises(TargetNotFound):
        surface.resolve(ladder(LabelRung(text="A"), TextRung(text="B")))


def test_hidden_elements_do_not_count(surface: WebSurface) -> None:
    surface.page.set_content(
        '<button style="display:none">Search</button><button>Search</button>'
    )
    resolved = surface.resolve(ladder(RoleRung(role="button", name="Search")))
    assert resolved.locator.is_visible()


def test_exact_text_matches_innermost_only(surface: WebSurface) -> None:
    surface.page.set_content("<table><tr><td><b>Member ID</b></td></tr></table>")
    resolved = surface.resolve(ladder(TextRung(text="Member ID")))
    assert resolved.locator.evaluate("n => n.tagName") == "B"


def test_role_name_is_exact_not_substring(surface: WebSurface) -> None:
    surface.page.set_content("<button>Search</button><button>Search Again</button>")
    resolved = surface.resolve(ladder(RoleRung(role="button", name="Search")))
    assert resolved.locator.inner_text() == "Search"


def test_cell_right_reads_table_soup(surface: WebSurface) -> None:
    surface.page.set_content(
        "<table><tr><td>Savings</td><td>$1,234.50</td></tr>"
        "<tr><td>Checking</td><td>$987.65</td></tr></table>"
    )
    resolved = surface.resolve(
        ladder(RelativeRung(relation="cell_right", anchor=TextRung(text="Savings")))
    )
    assert resolved.locator.inner_text() == "$1,234.50"


def test_cell_right_ignores_adjacent_rows_and_headers(surface: WebSurface) -> None:
    # Regression: a header cell in the same COLUMN sits one row above; a
    # center-distance row test would admit it. Same-row means vertical overlap.
    surface.page.set_content(
        "<table><tr><th>Account</th><th>Balance</th></tr>"
        "<tr><td>Savings</td><td>$1,234.50</td></tr>"
        "<tr><td>Checking</td><td>$987.65</td></tr></table>"
    )
    resolved = surface.resolve(
        ladder(RelativeRung(relation="cell_right", anchor=TextRung(text="Savings")))
    )
    assert resolved.locator.inner_text() == "$1,234.50"


def test_nearest_input_right_finds_unlabeled_input(surface: WebSurface) -> None:
    surface.page.set_content(
        "<table><tr><td>Member ID</td><td><input name='q1'></td></tr>"
        "<tr><td>Notes</td><td><input name='q2'></td></tr></table>"
    )
    resolved = surface.resolve(
        ladder(
            RelativeRung(relation="nearest_input_right", anchor=TextRung(text="Member ID"))
        )
    )
    assert resolved.locator.get_attribute("name") == "q1"


def test_geometric_tie_is_ambiguity_not_dom_order(surface: WebSurface) -> None:
    # A rowspan anchor sees one equally-near cell per spanned row. Picking the
    # DOM-order winner would smuggle a guess past the exactly-one rule.
    surface.page.set_content(
        "<table><tr><td rowspan='2'>Savings</td><td>$1.00</td></tr>"
        "<tr><td>$2.00</td></tr></table>"
    )
    with pytest.raises(TargetAmbiguous):
        surface.resolve(
            ladder(RelativeRung(relation="cell_right", anchor=TextRung(text="Savings")))
        )


def test_ambiguous_anchor_refuses(surface: WebSurface) -> None:
    surface.page.set_content(
        "<table><tr><td>Savings</td><td>$1.00</td></tr>"
        "<tr><td>Savings</td><td>$2.00</td></tr></table>"
    )
    with pytest.raises(TargetAmbiguous):
        surface.resolve(
            ladder(RelativeRung(relation="cell_right", anchor=TextRung(text="Savings")))
        )


def test_fingerprint_mismatch_fails_hard(surface: WebSurface) -> None:
    surface.page.set_content("<a href='#'>Search</a>")
    target = TargetLadder(
        ladder=[TextRung(text="Search")],
        fingerprint=Fingerprint(role="button"),
    )
    with pytest.raises(FingerprintMismatch):
        surface.resolve(target)


def test_label_rung_is_real_association_only(surface: WebSurface) -> None:
    # A td text label is NOT a label association; the ladder must fall through
    # to the proximity rung — this is the legacy-markup reality the design
    # documents (bare inputs have empty accessible names).
    surface.page.set_content(
        "<table><tr><td>Member ID</td><td><input name='q1'></td></tr></table>"
    )
    resolved = surface.resolve(
        ladder(
            LabelRung(text="Member ID"),
            RelativeRung(relation="nearest_input_right", anchor=TextRung(text="Member ID")),
        )
    )
    assert resolved.rung_index == 1
