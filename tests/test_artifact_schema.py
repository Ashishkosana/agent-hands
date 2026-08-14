"""Schema validator tests: the properties that make an artifact safe to replay
must be enforced at load time, not discovered at run time."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from pydantic import ValidationError

from hands.artifact import Capability, dump_capability
from tests.conftest import ARTIFACT_PATH

# A minimal, valid capability used as the mutation base for validator tests.
BASE: dict[str, Any] = {
    "schema_version": 1,
    "name": "example",
    "version": 1,
    "description": "example",
    "target": {"app": "app", "entry": {"web": {"url": "http://127.0.0.1:1/"}}},
    "parameters": {
        "member_id": {"type": "string", "description": "id", "example": "1"},
    },
    "outcomes": {"NOT_FOUND": {"description": "missing"}},
    "outputs": {},
    "regions": {
        "results": {"ladder": [{"strategy": "text", "text": "Results"}]},
        "header": {"ladder": [{"strategy": "text", "text": "Header"}]},
    },
    "steps": [
        {
            "id": "s1",
            "intent": "search",
            "action": {"kind": "type", "text": "{param:member_id}"},
            "target": {"ladder": [{"strategy": "label", "text": "Member ID"}]},
            "post": [{"kind": "value_matches_param", "param": "member_id"}],
            "risk": "safe",
        }
    ],
    "conditions": [
        {
            "id": "not_found",
            "armed_after": "s1",
            "match": {"kind": "region_text", "region": "results", "patterns": ["No match"]},
            "classify": "business_outcome",
            "outcome_code": "NOT_FOUND",
            "provenance": "authored",
        }
    ],
    "checkpoint": {
        "all": [{"kind": "region_text_matches_param", "region": "header", "param": "member_id"}]
    },
}


def build(mutate: dict[str, Any] | None = None) -> Capability:
    data = copy.deepcopy(BASE)
    if mutate:
        data.update(mutate)
    return Capability.model_validate(data)


def test_base_is_valid() -> None:
    build()


def test_design_doc_example_loads() -> None:
    """The design doc's canonical artifact example must validate against the
    real schema — the reviewable contract and the loader cannot drift apart."""
    design = (ARTIFACT_PATH.parent.parent / "docs" / "DESIGN.md").read_text()
    import re as _re

    block = _re.search(r"```json\n(.*?)```", design, _re.DOTALL)
    assert block is not None, "DESIGN.md has no json example"
    Capability.model_validate_json(block.group(1))


def test_repo_artifact_loads_and_round_trips() -> None:
    original = json.loads(ARTIFACT_PATH.read_text())
    cap = Capability.model_validate(original)
    again = Capability.model_validate_json(dump_capability(cap))
    assert cap == again


def test_undeclared_outcome_code_rejected() -> None:
    data = copy.deepcopy(BASE)
    data["conditions"][0]["outcome_code"] = "MYSTERY"
    with pytest.raises(ValidationError, match="undeclared outcome codes"):
        Capability.model_validate(data)


def test_unreachable_outcome_rejected() -> None:
    data = copy.deepcopy(BASE)
    data["outcomes"]["ORPHAN"] = {"description": "never produced"}
    with pytest.raises(ValidationError, match="no recognizer"):
        Capability.model_validate(data)


def test_unbound_checkpoint_rejected() -> None:
    data = copy.deepcopy(BASE)
    data["checkpoint"] = {"all": [{"kind": "text_visible", "text": "Done"}]}
    with pytest.raises(ValidationError, match="bind identity"):
        Capability.model_validate(data)


def test_unbound_checkpoint_allowed_with_waiver() -> None:
    data = copy.deepcopy(BASE)
    data["checkpoint"] = {"all": [{"kind": "text_visible", "text": "Done"}]}
    data["allow_unbound_checkpoint"] = True
    Capability.model_validate(data)


def test_sensitive_param_forbids_example() -> None:
    data = copy.deepcopy(BASE)
    data["parameters"]["member_id"] = {
        "type": "string",
        "description": "id",
        "sensitive": True,
        "example": "1",
    }
    with pytest.raises(ValidationError, match="must not carry an example"):
        Capability.model_validate(data)


def test_duplicate_step_ids_rejected() -> None:
    data = copy.deepcopy(BASE)
    data["steps"] = [data["steps"][0], copy.deepcopy(data["steps"][0])]
    with pytest.raises(ValidationError, match="unique"):
        Capability.model_validate(data)


def test_unknown_region_rejected() -> None:
    data = copy.deepcopy(BASE)
    data["conditions"][0]["match"]["region"] = "nowhere"
    with pytest.raises(ValidationError, match="unknown region"):
        Capability.model_validate(data)


def test_armed_after_unknown_step_rejected() -> None:
    data = copy.deepcopy(BASE)
    data["conditions"][0]["armed_after"] = "s99"
    with pytest.raises(ValidationError, match="unknown step"):
        Capability.model_validate(data)


def test_undeclared_placeholder_rejected() -> None:
    data = copy.deepcopy(BASE)
    data["steps"][0]["action"]["text"] = "{param:ghost}"
    with pytest.raises(ValidationError, match="undeclared parameter"):
        Capability.model_validate(data)


def test_type_action_requires_target() -> None:
    data = copy.deepcopy(BASE)
    del data["steps"][0]["target"]
    with pytest.raises(ValidationError, match="requires a target"):
        Capability.model_validate(data)


def test_decimal_output_requires_money_parse() -> None:
    data = copy.deepcopy(BASE)
    data["outputs"] = {
        "balance": {
            "type": "decimal",
            "from": {"ladder": [{"strategy": "text", "text": "Savings"}]},
        }
    }
    with pytest.raises(ValidationError, match="money parse"):
        Capability.model_validate(data)


def test_steps_require_postconditions() -> None:
    data = copy.deepcopy(BASE)
    data["steps"][0]["post"] = []
    with pytest.raises(ValidationError):
        Capability.model_validate(data)


def test_unknown_fields_rejected() -> None:
    data = copy.deepcopy(BASE)
    data["surprise"] = True
    with pytest.raises(ValidationError):
        Capability.model_validate(data)
