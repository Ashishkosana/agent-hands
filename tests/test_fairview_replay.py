"""Deterministic replay of the Fairview 7-function registry against the fixture.

No LLM. Intent Gate is disabled here so the irreversible Post Transfer click
can be proven end-to-end; test_intent_gate.py covers the attended gate itself.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from hands.artifact import load_capability
from hands.replay import EngineConfig, PolicySettings, ReplayEngine
from hands.results import BusinessOutcome, Success
from tests.conftest import FAIRVIEW_DIR, load_fairview

AUTH = {"operator_id": "teller1", "password": "password"}
SUPER = {"operator_id": "super1", "password": "password"}


def engine(tmp_path: Path, *, intent: bool | None = False) -> ReplayEngine:
    return ReplayEngine(
        EngineConfig(
            runs_dir=tmp_path / "runs",
            policy=PolicySettings(require_intent_approval=intent),
        )
    )


def test_generated_fairview_artifacts_load() -> None:
    names = [
        "fairview_sign_on",
        "fairview_member_inquiry",
        "fairview_member_balance",
        "fairview_funds_transfer",
        "fairview_open_new_share",
        "fairview_update_contact",
        "fairview_place_hold",
    ]
    for name in names:
        cap = load_capability(FAIRVIEW_DIR / f"{name}.json")
        assert cap.name == name
        assert cap.target.app == "fairview-teller"


def test_sign_on_replay(fixture_app: str, tmp_path: Path) -> None:
    cap = load_fairview("fairview_sign_on", fixture_app, sign=True)
    result = engine(tmp_path).run(cap, AUTH)
    assert isinstance(result, Success)


def test_sign_on_failed_is_an_answer(fixture_app: str, tmp_path: Path) -> None:
    cap = load_fairview("fairview_sign_on", fixture_app, sign=True)
    result = engine(tmp_path).run(cap, {"operator_id": "teller1", "password": "wrongpass"})
    assert isinstance(result, BusinessOutcome)
    assert result.code == "SIGNON_FAILED"


def test_member_inquiry_by_last_name(fixture_app: str, tmp_path: Path) -> None:
    cap = load_fairview("fairview_member_inquiry", fixture_app, sign=True)
    result = engine(tmp_path).run(cap, {**AUTH, "last_name": "Ruiz"})
    assert isinstance(result, Success)
    assert result.outputs == {"member_id": "12345", "member_name": "Pat Ruiz"}


def test_member_inquiry_not_found(fixture_app: str, tmp_path: Path) -> None:
    cap = load_fairview("fairview_member_inquiry", fixture_app, sign=True)
    result = engine(tmp_path).run(cap, {**AUTH, "last_name": "Nobody"})
    assert isinstance(result, BusinessOutcome)
    assert result.code == "MEMBER_NOT_FOUND"


def test_member_balance_replay(fixture_app: str, tmp_path: Path) -> None:
    cap = load_fairview("fairview_member_balance", fixture_app, sign=True)
    result = engine(tmp_path).run(cap, {**AUTH, "member_id": "12345"})
    assert isinstance(result, Success)
    assert result.outputs["member_name"] == "Pat Ruiz"
    assert result.outputs["member_status"] == "Active"
    assert result.outputs["savings_balance"] == Decimal("1234.50")


def test_funds_transfer_replay(fixture_app: str, tmp_path: Path) -> None:
    cap = load_fairview("fairview_funds_transfer", fixture_app, sign=True)
    result = engine(tmp_path).run(
        cap,
        {
            **AUTH,
            "member_id": "12345",
            "from_share": "S1 - Savings",
            "to_share": "S2 - Checking",
            "amount": "1.00",
            "memo": "replay demo",
        },
    )
    assert isinstance(result, Success)
    assert str(result.outputs["confirmation_number"]).startswith("CN-")


def test_funds_transfer_validation_rejected(fixture_app: str, tmp_path: Path) -> None:
    cap = load_fairview("fairview_funds_transfer", fixture_app, sign=True)
    result = engine(tmp_path).run(
        cap,
        {
            **AUTH,
            "member_id": "12345",
            "from_share": "S1 - Savings",
            "to_share": "S2 - Checking",
            "amount": "99999.00",
            "memo": "too much",
        },
    )
    assert isinstance(result, BusinessOutcome)
    assert result.code == "VALIDATION_REJECTED"


def test_open_new_share_replay(fixture_app: str, tmp_path: Path) -> None:
    cap = load_fairview("fairview_open_new_share", fixture_app, sign=True)
    result = engine(tmp_path).run(
        cap,
        {
            **AUTH,
            "member_id": "12345",
            "share_type": "Money Market",
            "initial_deposit": "10.00",
        },
    )
    assert isinstance(result, Success)
    assert str(result.outputs["confirmation_number"]).startswith("CN-")


def test_update_contact_replay(fixture_app: str, tmp_path: Path) -> None:
    cap = load_fairview("fairview_update_contact", fixture_app, sign=True)
    result = engine(tmp_path).run(
        cap,
        {
            **AUTH,
            "member_id": "12345",
            "email": "pat.ruiz@example.net",
            "phone": "555-0199",
            "address": "99 Test Ave, Fairview",
        },
    )
    assert isinstance(result, Success)


def test_place_hold_teller_is_supervisor_required(fixture_app: str, tmp_path: Path) -> None:
    cap = load_fairview("fairview_place_hold", fixture_app, sign=True)
    result = engine(tmp_path).run(
        cap,
        {
            **AUTH,
            "member_id": "12345",
            "share": "S1 - Savings",
            "reason": "FRAUD",
            "notes": "requested by member",
        },
    )
    assert isinstance(result, BusinessOutcome)
    assert result.code == "SUPERVISOR_REQUIRED"


def test_place_hold_supervisor_replay(fixture_app: str, tmp_path: Path) -> None:
    cap = load_fairview("fairview_place_hold", fixture_app, sign=True)
    result = engine(tmp_path).run(
        cap,
        {
            **SUPER,
            "member_id": "12345",
            "share": "S1 - Savings",
            "reason": "FRAUD",
            "notes": "requested by member",
        },
    )
    assert isinstance(result, Success)
    assert str(result.outputs["confirmation_number"]).startswith("CN-")
