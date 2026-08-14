"""Value handling: parameter placeholder resolution, normalization, typed parsing.

UIs reformat what they display ("1000" becomes "$1,000.00"), so comparisons and
extractions are type-aware rather than exact-string. A parse failure is an
error, never a value.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Literal

PLACEHOLDER_RE = re.compile(r"\{param:([A-Za-z_][A-Za-z0-9_]*)\}")

Normalize = Literal["none", "digits", "money"]


class ValueError_(Exception):
    """Raised for placeholder/parse problems. Named to avoid shadowing builtins."""


def placeholders_in(text: str) -> list[str]:
    """Parameter names referenced by ``{param:name}`` placeholders in *text*."""
    return PLACEHOLDER_RE.findall(text)


def resolve_text(text: str, params: dict[str, str]) -> str:
    """Substitute ``{param:name}`` placeholders with concrete values."""

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in params:
            raise ValueError_(f"text references undeclared parameter {name!r}")
        return params[name]

    return PLACEHOLDER_RE.sub(_sub, text)


def normalize(value: str, mode: Normalize) -> str:
    """Normalize a value for comparison. UIs echo back reformatted input, so
    postconditions compare normalized forms, not raw strings."""
    if mode == "none":
        return value.strip()
    if mode == "digits":
        return re.sub(r"\D", "", value)
    # money: canonical string form of the parsed decimal
    return str(parse_money(value))


_MONEY_STRIP_RE = re.compile(r"[$,\s\u00a0]")  # \u00a0: legacy pages pad with nbsp


def parse_money(text: str) -> Decimal:
    """Parse a US-locale money rendering into a Decimal.

    Handles: "$1,234.50", "(1,234.50)" (parenthesized negative), "1,234.50-"
    (trailing-minus negative), leading minus, and stray whitespace/nbsp.
    """
    cleaned = _MONEY_STRIP_RE.sub("", text)
    negative = False
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = cleaned[1:-1]
        negative = True
    if cleaned.endswith("-"):
        cleaned = cleaned[:-1]
        negative = True
    if cleaned.startswith("-"):
        cleaned = cleaned[1:]
        negative = True
    if not cleaned:
        raise ValueError_(f"cannot parse money value from {text!r}")
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError_(f"cannot parse money value from {text!r}") from exc
    return -value if negative else value
