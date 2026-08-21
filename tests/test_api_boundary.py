"""API boundary guardrails: the currency input sanitizer and idempotency keys.

The engine is stubbed — these test the WRAPPER's contract (sanitize, suppress
duplicate execution, envelope shape) without a browser. Import purity of the
API (no model code) is asserted the same way the replay path's is.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from hands.api import _sanitize_value, create_api
from hands.artifact import load_capability
from hands.results import Success

ARTIFACT_DIR = Path(__file__).resolve().parent.parent / "capabilities" / "generated"
CAP = load_capability(ARTIFACT_DIR / "meridian_funds_transfer.json")


class TestCurrencySanitizer:
    def test_currency_dressing_is_stripped(self) -> None:
        assert _sanitize_value(CAP, "amount", "$1,000.50") == "1000.50"
        assert _sanitize_value(CAP, "amount", " 25 ") == "25"
        assert _sanitize_value(CAP, "amount", "$5") == "5"

    def test_plain_values_pass_through(self) -> None:
        assert _sanitize_value(CAP, "amount", "10.25") == "10.25"
        assert _sanitize_value(CAP, "member_number", "100234") == "100234"

    def test_non_currency_text_is_untouched(self) -> None:
        # A memo like "$$$ URGENT" is not unambiguous currency — never rewritten.
        assert _sanitize_value(CAP, "to_share", "S0001-3") == "S0001-3"
        assert _sanitize_value(CAP, "from_share", "Share Draft (Checking)") == (
            "Share Draft (Checking)"
        )

    def test_sensitive_params_are_never_rewritten(self) -> None:
        # A password that happens to look like currency must reach the engine verbatim.
        assert _sanitize_value(CAP, "password", "$1,234") == "$1,234"


class TestIdempotency:
    def _client_with_stub(self, calls: list[dict[str, str]]) -> Any:
        app = create_api(gen_dir=ARTIFACT_DIR, runs_dir=Path("runs"))
        app.config["TESTING"] = True

        class StubEngine:
            last_run_dir = None

            def run(self, cap: Any, params: dict[str, str]) -> Success:
                calls.append(dict(params))
                return Success(outputs={"confirmation_number": f"CN{len(calls):04d}"})

        # swap the real engine out from under the closure
        for cell in app.view_functions["invoke"].__closure__ or []:
            if type(cell.cell_contents).__name__ == "ReplayEngine":
                cell.cell_contents = StubEngine()
        return app.test_client()

    def test_same_key_returns_original_without_reexecuting(self) -> None:
        calls: list[dict[str, str]] = []
        client = self._client_with_stub(calls)
        body = {
            "params": {"operator_id": "teller1", "member_number": "100987",
                       "from_share": "MMKT-11", "to_share": "MMKT-5", "amount": "1"},
            "idempotency_key": "txn-777",
        }
        first = client.post("/capabilities/meridian_funds_transfer/invoke", json=body)
        second = client.post("/capabilities/meridian_funds_transfer/invoke", json=body)
        assert first.status_code == 200 and second.status_code == 200
        assert len(calls) == 1  # the transfer executed exactly once
        assert second.get_json()["idempotent_replay"] is True
        assert (second.get_json()["result"]["outputs"]
                == first.get_json()["result"]["outputs"])

    def test_different_keys_execute_independently(self) -> None:
        calls: list[dict[str, str]] = []
        client = self._client_with_stub(calls)
        base = {"params": {"operator_id": "teller1", "member_number": "100987",
                           "from_share": "MMKT-11", "to_share": "MMKT-5", "amount": "1"}}
        client.post("/capabilities/meridian_funds_transfer/invoke",
                    json={**base, "idempotency_key": "txn-1"})
        client.post("/capabilities/meridian_funds_transfer/invoke",
                    json={**base, "idempotency_key": "txn-2"})
        assert len(calls) == 2


def test_api_imports_no_model_code() -> None:
    banned = ("hands.llm", "hands.planner", "hands.discover", "hands.observe",
              "hands.recorder")
    loaded = [m for m in banned if m in sys.modules]
    assert not loaded, f"model code reachable from the API import graph: {loaded}"
