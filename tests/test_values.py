from __future__ import annotations

from decimal import Decimal

import pytest

from hands.values import (
    ValueError_,
    normalize,
    param_value_matches,
    parse_money,
    placeholders_in,
    resolve_text,
    value_bound_in,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$1,234.50", Decimal("1234.50")),
        ("1234.50", Decimal("1234.50")),
        ("$0.00", Decimal("0.00")),
        ("(1,234.50)", Decimal("-1234.50")),
        ("1,234.50-", Decimal("-1234.50")),
        ("-$99.10", Decimal("-99.10")),
        ("$3,410.19", Decimal("3410.19")),
    ],
)
def test_parse_money(text: str, expected: Decimal) -> None:
    assert parse_money(text) == expected


@pytest.mark.parametrize("text", ["", "N/A", "--", "$"])
def test_parse_money_rejects_junk(text: str) -> None:
    with pytest.raises(ValueError_):
        parse_money(text)


@pytest.mark.parametrize(
    "text",
    [
        "1.234,50",  # comma-decimal locale: must FAIL, not parse 100x wrong
        "12 345,67",
        "NaN",  # Decimal() would accept these; money must not
        "Infinity",
        "sNaN",
        "1_000",
        "1,23.45",  # malformed grouping
        "12,34",
    ],
)
def test_parse_money_rejects_rather_than_misparses(text: str) -> None:
    with pytest.raises(ValueError_):
        parse_money(text)


def test_value_bound_in_requires_boundaries() -> None:
    # Identity binding: "123" must not pass against member 12345's page.
    assert not value_bound_in("123", "Member # 12345")
    assert value_bound_in("12345", "Member # 12345")
    assert value_bound_in("12345", "ID:12345.")
    assert not value_bound_in("", "anything")


def test_param_value_matches_never_vacuous() -> None:
    # A digitless expected value normalizes to "" and can never match — an
    # empty field must not satisfy a value postcondition vacuously.
    assert not param_value_matches("abc", "", "digits")
    assert param_value_matches("12-345", "12345", "digits")
    assert not param_value_matches("1000", "junk", "money")


def test_normalize_digits() -> None:
    assert normalize("12-345 ", "digits") == "12345"


def test_normalize_money_is_canonical() -> None:
    assert normalize("$1,000.00", "money") == normalize("1000.00", "money")


def test_resolve_text_substitutes() -> None:
    assert resolve_text("{param:member_id}", {"member_id": "12345"}) == "12345"


def test_resolve_text_unknown_param_raises() -> None:
    with pytest.raises(ValueError_):
        resolve_text("{param:nope}", {"member_id": "12345"})


def test_placeholders_in() -> None:
    assert placeholders_in("a {param:x} b {param:y_2}") == ["x", "y_2"]
