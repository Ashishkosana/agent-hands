from __future__ import annotations

from decimal import Decimal

import pytest

from hands.values import ValueError_, normalize, parse_money, placeholders_in, resolve_text


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
