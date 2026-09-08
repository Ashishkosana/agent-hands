"""Author the Fairview Teller 7-function capability registry.

Hand-authored (Path B), matching the fixture UI. Everything is built through
the Pydantic schema so outcome closure, identity binding, and param usage are
enforced at authoring time. Risky artifacts are signed so unattended replay
is unlocked; funds transfer still hits the per-run Transfer Intent Gate.

Run:  python scripts/build_fairview_capabilities.py
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from hands.artifact import Capability, dump_capability, sign_risk_review

GEN = Path("capabilities/generated")

MAIN: list[dict[str, Any]] = [{"name": "main", "url_pattern": None, "ordinal": None}]
NAV: list[dict[str, Any]] = [{"name": "nav", "url_pattern": None, "ordinal": None}]

TARGET = {
    "app": "fairview-teller",
    "entry": {"web": {"url": "http://127.0.0.1:8000/"}},
}
REQUIRES = [
    {
        "kind": "role_name_visible",
        "role": "heading",
        "name": "Fairview Teller Console",
        "context": MAIN,
    }
]

P_OPERATOR = {
    "type": "string",
    "description": "Operator sign-on ID",
    "sensitive": False,
    "example": "teller1",
    "pattern": "[a-zA-Z0-9_]{1,32}",
}
P_PASSWORD = {
    "type": "string",
    "description": "Operator password",
    "sensitive": True,
    "example": None,
    "pattern": None,
}
P_MEMBER = {
    "type": "string",
    "description": "Member number as printed on statements",
    "sensitive": False,
    "example": "12345",
    "pattern": "[0-9]{1,10}",
}


def heading(name: str, context: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "kind": "role_name_visible",
        "role": "heading",
        "name": name,
        "context": context if context is not None else MAIN,
    }


def type_step(
    id_: str,
    intent: str,
    label: str,
    param: str,
    *,
    context: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    ctx = context if context is not None else MAIN
    return {
        "id": id_,
        "intent": intent,
        "action": {"kind": "type", "text": f"{{param:{param}}}"},
        "target": {
            "ladder": [
                {"strategy": "label", "text": label},
                {
                    "strategy": "relative",
                    "relation": "nearest_input_right",
                    "anchor": {"strategy": "text", "text": label},
                    "container": None,
                },
            ],
            "context": ctx,
            "fingerprint": {"role": "textbox", "editable": True},
            "fragile": False,
        },
        "pre": [{"kind": "editable"}],
        "post": [{"kind": "value_matches_param", "param": param, "normalize": "none"}],
        "risk": "safe",
    }


def select_step(
    id_: str,
    intent: str,
    label: str,
    option: str,
    match: str = "contains",
) -> dict[str, Any]:
    return {
        "id": id_,
        "intent": intent,
        "action": {"kind": "select", "option": option, "match": match},
        "target": {
            "ladder": [
                {
                    "strategy": "relative",
                    "relation": "nearest_input_right",
                    "anchor": {"strategy": "text", "text": label},
                    "container": None,
                }
            ],
            "context": MAIN,
            "fingerprint": {"role": "combobox", "editable": None},
            "fragile": False,
        },
        "pre": [{"kind": "visible"}],
        "post": [{"kind": "option_selected", "option": option, "match": match}],
        "risk": "safe",
    }


def click_step(
    id_: str,
    intent: str,
    name: str,
    role: str,
    *,
    heading_name: str | None = None,
    post: list[dict[str, Any]] | None = None,
    context: list[dict[str, Any]] | None = None,
    post_context: list[dict[str, Any]] | None = None,
    risky: bool = False,
) -> dict[str, Any]:
    ctx = context if context is not None else MAIN
    pctx = post_context if post_context is not None else MAIN
    if post is None:
        if heading_name is None:
            raise ValueError("click_step needs heading_name or post")
        post = [heading(heading_name, pctx)]
    return {
        "id": id_,
        "intent": intent,
        "action": {"kind": "click"},
        "target": {
            "ladder": [
                {"strategy": "role", "role": role, "name": name},
                {"strategy": "text", "text": name},
            ],
            "context": ctx,
            "fingerprint": {"role": role, "editable": None},
            "fragile": False,
        },
        "pre": [{"kind": "visible"}],
        "post": post,
        "risk": "risky" if risky else "safe",
    }


def text_output(anchor: str, *, region: str, sensitive: bool = False) -> dict[str, Any]:
    return {
        "type": "string",
        "sensitive": sensitive,
        "region": region,
        "from": {
            "ladder": [
                {
                    "strategy": "relative",
                    "relation": "cell_right",
                    "anchor": {"strategy": "text", "text": anchor},
                    "container": None,
                }
            ],
            "context": MAIN,
            "fingerprint": None,
            "fragile": False,
        },
        "parse": {"kind": "text"},
    }


def money_output(anchor: str, *, region: str = "accounts") -> dict[str, Any]:
    return {
        "type": "decimal",
        "sensitive": True,
        "region": region,
        "from": {
            "ladder": [
                {
                    "strategy": "relative",
                    "relation": "cell_right",
                    "anchor": {"strategy": "text", "text": anchor},
                    "container": None,
                }
            ],
            "context": MAIN,
            "fingerprint": None,
            "fragile": False,
        },
        "parse": {"kind": "money", "locale": "en_US"},
    }


def table_region(anchor: str) -> dict[str, Any]:
    return {
        "ladder": [
            {
                "strategy": "relative",
                "relation": "container_of",
                "anchor": {"strategy": "text", "text": anchor},
                "container": "table",
            }
        ],
        "context": MAIN,
        "fingerprint": None,
        "fragile": False,
    }


def outcome_cond(
    id_: str,
    armed_after: str,
    *,
    region: str,
    patterns: list[str],
    code: str,
) -> dict[str, Any]:
    return {
        "id": id_,
        "armed_after": armed_after,
        "match": {"kind": "region_text", "region": region, "patterns": patterns},
        "classify": "business_outcome",
        "outcome_code": code,
        "recovery": [],
        "resume": None,
        "restart_from": None,
        "max_fires_per_run": 2,
        "provenance": "authored",
        "verified_by_eval": False,
    }


def role_cond(
    id_: str,
    armed_after: str,
    *,
    role: str,
    name: str,
    code: str,
) -> dict[str, Any]:
    return {
        "id": id_,
        "armed_after": armed_after,
        "match": {"kind": "role_name", "role": role, "name": name, "context": MAIN},
        "classify": "business_outcome",
        "outcome_code": code,
        "recovery": [],
        "resume": None,
        "restart_from": None,
        "max_fires_per_run": 2,
        "provenance": "authored",
        "verified_by_eval": False,
    }


def capability(**kw: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema_version": 1,
        "version": 1,
        "target": TARGET,
        "requires": REQUIRES,
        "regions": {},
        "conditions": [],
        "allow_unbound_checkpoint": False,
        "recorded_by": None,
        "risk_review": {"reviewed_by": None, "artifact_hash": None},
        "outcomes": {},
        "outputs": {},
    }
    doc.update(kw)
    return doc


# Sign-on prefix: nav Sign On -> credentials -> Teller Menu.
SIGN_ON: list[dict[str, Any]] = [
    click_step(
        "s1",
        "Open the sign-on form",
        "Sign On",
        "link",
        heading_name="Operator Sign On",
        context=NAV,
        post_context=MAIN,
    ),
    type_step("s2", "Enter the operator ID", "Operator ID", "operator_id"),
    type_step("s3", "Enter the password", "Password", "password"),
    click_step(
        "s4",
        "Click Sign On",
        "Sign On",
        "button",
        heading_name="Teller Menu",
        risky=True,
    ),
]

TO_MEMBER: list[dict[str, Any]] = [
    *SIGN_ON,
    click_step(
        "s5",
        "Open member inquiry",
        "Member Inquiry",
        "link",
        heading_name="Member Inquiry",
    ),
    type_step("s6", "Enter the member ID", "Member ID", "member_id"),
    click_step("s7", "Submit the member search", "Search", "button", heading_name="Member Details"),
]

MEMBER_HEADER = table_region("Member #")
ACCOUNTS = table_region("Account")
SEARCH_RESULTS = table_region("Search Results")
INPUT_ERRORS = table_region("Input Errors")
SUPERVISOR = table_region("SUPERVISOR OVERRIDE REQUIRED")
CONFIRMATION = table_region("Confirmation:")
CONTACT_UPDATED = table_region("MEMBER INFORMATION UPDATED")

NOT_FOUND = outcome_cond(
    "not_found",
    "s7",
    region="results",
    patterns=["No member matches"],
    code="MEMBER_NOT_FOUND",
)
SIGNON_FAILED = outcome_cond(
    "signon_failed",
    "s4",
    region="signon_error",
    patterns=["Operator ID or password is not recognized"],
    code="SIGNON_FAILED",
)

ARTIFACTS: dict[str, dict[str, Any]] = {}

ARTIFACTS["fairview_sign_on"] = capability(
    name="fairview_sign_on",
    description=(
        "Sign on to the Fairview Teller Console and verify the session is active "
        "(Teller Menu is reachable)."
    ),
    parameters={"operator_id": P_OPERATOR, "password": P_PASSWORD},
    steps=SIGN_ON,
    regions={"signon_error": table_region("Sign-on failed")},
    outcomes={"SIGNON_FAILED": {"description": "Operator ID or password was rejected."}},
    conditions=[SIGNON_FAILED],
    checkpoint={"all": [heading("Teller Menu")]},
    allow_unbound_checkpoint=True,
)

ARTIFACTS["fairview_member_inquiry"] = capability(
    name="fairview_member_inquiry",
    description=(
        "Sign on, search the member directory by last name, and return the "
        "matching member number and name."
    ),
    parameters={
        "operator_id": P_OPERATOR,
        "password": P_PASSWORD,
        "last_name": {
            "type": "string",
            "description": "Member surname to search",
            "sensitive": False,
            "example": "Ruiz",
            "pattern": "[A-Za-z' -]{1,40}",
        },
    },
    steps=[
        *SIGN_ON,
        click_step(
            "s5",
            "Open member inquiry",
            "Member Inquiry",
            "link",
            heading_name="Member Inquiry",
        ),
        type_step("s6", "Enter the last name", "Last Name", "last_name"),
        click_step(
            "s7", "Submit the member search", "Search", "button", heading_name="Member Details"
        ),
    ],
    regions={
        "identity": MEMBER_HEADER,
        "results": SEARCH_RESULTS,
        "signon_error": table_region("Sign-on failed"),
    },
    outcomes={
        "MEMBER_NOT_FOUND": {"description": "No member matches this surname."},
        "SIGNON_FAILED": {"description": "Operator ID or password was rejected."},
    },
    conditions=[
        SIGNON_FAILED,
        outcome_cond(
            "not_found",
            "s7",
            region="results",
            patterns=["No member matches"],
            code="MEMBER_NOT_FOUND",
        ),
    ],
    outputs={
        "member_id": text_output("Member #", region="identity"),
        "member_name": text_output("Name", region="identity"),
    },
    checkpoint={
        "all": [
            heading("Member Details"),
            {"kind": "region_text_matches_param", "region": "identity", "param": "last_name"},
        ]
    },
)

ARTIFACTS["fairview_member_balance"] = capability(
    name="fairview_member_balance",
    description=(
        "Sign on, look up a member by ID, and read name, status, and savings balance."
    ),
    parameters={
        "operator_id": P_OPERATOR,
        "password": P_PASSWORD,
        "member_id": P_MEMBER,
    },
    steps=TO_MEMBER,
    regions={
        "identity": MEMBER_HEADER,
        "accounts": ACCOUNTS,
        "results": SEARCH_RESULTS,
        "input_errors": INPUT_ERRORS,
        "signon_error": table_region("Sign-on failed"),
    },
    outcomes={
        "MEMBER_NOT_FOUND": {"description": "No member exists with this ID."},
        "VALIDATION_REJECTED": {
            "description": "The application rejected the member ID as invalid input."
        },
        "SIGNON_FAILED": {"description": "Operator ID or password was rejected."},
    },
    conditions=[
        SIGNON_FAILED,
        NOT_FOUND,
        outcome_cond(
            "validation_rejected",
            "s7",
            region="input_errors",
            patterns=["Member ID must be numeric."],
            code="VALIDATION_REJECTED",
        ),
    ],
    outputs={
        "member_name": text_output("Name", region="identity"),
        "member_status": text_output("Status", region="identity"),
        "savings_balance": money_output("Savings"),
    },
    checkpoint={
        "all": [
            heading("Member Details"),
            {"kind": "region_text_matches_param", "region": "identity", "param": "member_id"},
        ]
    },
)

ARTIFACTS["fairview_funds_transfer"] = capability(
    name="fairview_funds_transfer",
    description=(
        "Sign on, look up a member, and transfer funds between two of their shares "
        "(review, then Post Transfer)."
    ),
    parameters={
        "operator_id": P_OPERATOR,
        "password": P_PASSWORD,
        "member_id": P_MEMBER,
        "from_share": {
            "type": "string",
            "description": (
                "Stable unique substring identifying the source share, e.g. 'S1 - Savings'"
            ),
            "sensitive": False,
            "example": "S1 - Savings",
            "pattern": None,
        },
        "to_share": {
            "type": "string",
            "description": (
                "Stable unique substring identifying the destination share, e.g. 'S2 - Checking'"
            ),
            "sensitive": False,
            "example": "S2 - Checking",
            "pattern": None,
        },
        "amount": {
            "type": "string",
            "description": "Dollar amount to transfer",
            "sensitive": False,
            "example": "1.00",
            "pattern": r"[0-9]+(\.[0-9]{1,2})?",
        },
        "memo": {
            "type": "string",
            "description": "Transfer memo",
            "sensitive": False,
            "example": "counter credit",
            "pattern": None,
        },
    },
    steps=[
        *TO_MEMBER,
        click_step(
            "s8",
            "Open the funds transfer form",
            "Funds Transfer",
            "link",
            heading_name="Funds Transfer",
        ),
        select_step("s9", "Choose the source share", "From Share", "{param:from_share}"),
        select_step("s10", "Choose the destination share", "To Share", "{param:to_share}"),
        type_step("s11", "Enter the transfer amount", "Amount", "amount"),
        type_step("s12", "Enter the memo", "Memo", "memo"),
        {
            **click_step(
                "s13",
                "Continue to the transfer review",
                "Continue",
                "button",
                heading_name="Transfer Review",
            ),
            "post": [
                heading("Transfer Review"),
                {
                    "kind": "region_text_matches_param",
                    "region": "confirm_member",
                    "param": "member_id",
                },
            ],
        },
        click_step(
            "s14",
            "Post the transfer",
            "Post Transfer",
            "button",
            heading_name="Transfer Posted",
            risky=True,
        ),
    ],
    regions={
        "identity": CONFIRMATION,
        "transfer_result": CONFIRMATION,
        "confirm_member": MEMBER_HEADER,
        "results": SEARCH_RESULTS,
        "signon_error": table_region("Sign-on failed"),
    },
    outcomes={
        "VALIDATION_REJECTED": {
            "description": (
                "The core rejected the transfer (insufficient funds, hold, or invalid amount)."
            ),
        },
        "MEMBER_NOT_FOUND": {"description": "No member exists with this ID."},
        "SIGNON_FAILED": {"description": "Operator ID or password was rejected."},
    },
    conditions=[
        SIGNON_FAILED,
        {**NOT_FOUND, "armed_after": "s7"},
        role_cond(
            "validation_rejected",
            "s13",
            role="heading",
            name="Transaction Rejected",
            code="VALIDATION_REJECTED",
        ),
    ],
    outputs={"confirmation_number": text_output("Confirmation:", region="transfer_result")},
    checkpoint={
        "all": [
            heading("Transfer Posted"),
            {"kind": "region_text_matches_param", "region": "identity", "param": "member_id"},
        ]
    },
)

ARTIFACTS["fairview_open_new_share"] = capability(
    name="fairview_open_new_share",
    description=(
        "Sign on, open a member's record, and open a new share of the given type "
        "with an initial deposit (review, then post)."
    ),
    parameters={
        "operator_id": P_OPERATOR,
        "password": P_PASSWORD,
        "member_id": P_MEMBER,
        "share_type": {
            "type": "string",
            "description": "Share type option, e.g. 'Money Market'",
            "sensitive": False,
            "example": "Money Market",
            "pattern": None,
        },
        "initial_deposit": {
            "type": "string",
            "description": "Initial deposit in dollars",
            "sensitive": False,
            "example": "10.00",
            "pattern": r"[0-9]+(\.[0-9]{1,2})?",
        },
    },
    steps=[
        *TO_MEMBER,
        click_step(
            "s8",
            "Open the new share form",
            "Open New Share",
            "link",
            heading_name="Open New Share",
        ),
        select_step("s9", "Choose the share type", "Share Type", "{param:share_type}"),
        type_step("s10", "Enter the initial deposit", "Initial Deposit", "initial_deposit"),
        click_step(
            "s11",
            "Continue to the confirmation screen",
            "Continue",
            "button",
            heading_name="Confirm New Share",
        ),
        click_step(
            "s12",
            "Post the new share",
            "Post New Share",
            "button",
            heading_name="Share Opened",
            risky=True,
        ),
    ],
    regions={
        "identity": CONFIRMATION,
        "results": SEARCH_RESULTS,
        "signon_error": table_region("Sign-on failed"),
    },
    outcomes={
        "VALIDATION_REJECTED": {
            "description": "The core rejected the request (e.g. below the minimum opening deposit)."
        },
        "MEMBER_NOT_FOUND": {"description": "No member exists with this ID."},
        "SIGNON_FAILED": {"description": "Operator ID or password was rejected."},
    },
    conditions=[
        SIGNON_FAILED,
        {**NOT_FOUND, "armed_after": "s7"},
        role_cond(
            "validation_rejected",
            "s11",
            role="heading",
            name="Request Rejected",
            code="VALIDATION_REJECTED",
        ),
    ],
    outputs={"confirmation_number": text_output("Confirmation:", region="identity")},
    checkpoint={
        "all": [
            heading("Share Opened"),
            {"kind": "region_text_matches_param", "region": "identity", "param": "member_id"},
        ]
    },
)

ARTIFACTS["fairview_update_contact"] = capability(
    name="fairview_update_contact",
    description=(
        "Sign on, open a member's record, and update their e-mail, phone, and mailing address."
    ),
    parameters={
        "operator_id": P_OPERATOR,
        "password": P_PASSWORD,
        "member_id": P_MEMBER,
        "email": {
            "type": "string",
            "description": "New e-mail address",
            "sensitive": False,
            "example": "pat.ruiz@example.net",
            "pattern": r"[^@\s]+@[^@\s]+\.[^@\s]+",
        },
        "phone": {
            "type": "string",
            "description": "New phone number",
            "sensitive": False,
            "example": "555-0199",
            "pattern": r"[0-9() +.-]{7,20}",
        },
        "address": {
            "type": "string",
            "description": "New mailing address",
            "sensitive": False,
            "example": "99 Test Ave, Fairview",
            "pattern": None,
        },
    },
    steps=[
        *TO_MEMBER,
        click_step(
            "s8",
            "Open the Update Member Information form",
            "Update Member Information",
            "link",
            heading_name="Update Member Information",
        ),
        type_step("s9", "Enter the new e-mail", "E-mail", "email"),
        type_step("s10", "Enter the new phone", "Phone", "phone"),
        type_step("s11", "Enter the new mailing address", "Mailing Address", "address"),
        click_step(
            "s12",
            "Save the updated contact details",
            "Save Changes",
            "button",
            heading_name="Member Information Updated",
            risky=True,
        ),
    ],
    regions={
        "identity": CONTACT_UPDATED,
        "results": SEARCH_RESULTS,
        "signon_error": table_region("Sign-on failed"),
    },
    outcomes={
        "MEMBER_NOT_FOUND": {"description": "No member exists with this ID."},
        "SIGNON_FAILED": {"description": "Operator ID or password was rejected."},
    },
    conditions=[SIGNON_FAILED, {**NOT_FOUND, "armed_after": "s7"}],
    outputs={},
    checkpoint={
        "all": [
            heading("Member Information Updated"),
            {"kind": "region_text_matches_param", "region": "identity", "param": "member_id"},
        ]
    },
)

ARTIFACTS["fairview_place_hold"] = capability(
    name="fairview_place_hold",
    description=(
        "Sign on, open a member's record, and place a hold on one of their shares "
        "(review, then post). Supervisor-only: a teller attempt returns SUPERVISOR_REQUIRED."
    ),
    parameters={
        "operator_id": P_OPERATOR,
        "password": P_PASSWORD,
        "member_id": P_MEMBER,
        "share": {
            "type": "string",
            "description": "Stable substring identifying the share, e.g. 'S1 - Savings'",
            "sensitive": False,
            "example": "S1 - Savings",
            "pattern": None,
        },
        "reason": {
            "type": "string",
            "description": "Hold reason option, e.g. 'FRAUD'",
            "sensitive": False,
            "example": "FRAUD",
            "pattern": None,
        },
        "notes": {
            "type": "string",
            "description": "Hold notes",
            "sensitive": False,
            "example": "requested by member",
            "pattern": None,
        },
    },
    steps=[
        *TO_MEMBER,
        click_step(
            "s8",
            "Open the Place Account Hold form",
            "Place Account Hold",
            "link",
            heading_name="Place Account Hold",
        ),
        select_step("s9", "Choose the share to hold", "Share", "{param:share}"),
        select_step("s10", "Choose the reason code", "Reason", "{param:reason}"),
        type_step("s11", "Enter the hold notes", "Notes", "notes"),
        {
            **click_step(
                "s12",
                "Continue to the confirmation screen",
                "Continue",
                "button",
                heading_name="Confirm Account Hold",
            ),
            "post": [
                heading("Confirm Account Hold"),
                {
                    "kind": "region_text_matches_param",
                    "region": "confirm_member",
                    "param": "member_id",
                },
            ],
        },
        click_step(
            "s13",
            "Apply the hold",
            "Apply Hold",
            "button",
            heading_name="Account Hold Applied",
            risky=True,
        ),
    ],
    regions={
        "confirm_member": MEMBER_HEADER,
        "identity": CONFIRMATION,
        "outcome_supervisor": SUPERVISOR,
        "results": SEARCH_RESULTS,
        "signon_error": table_region("Sign-on failed"),
    },
    outcomes={
        "SUPERVISOR_REQUIRED": {
            "description": "This operator is not authorized; a supervisor must complete the hold."
        },
        "MEMBER_NOT_FOUND": {"description": "No member exists with this ID."},
        "SIGNON_FAILED": {"description": "Operator ID or password was rejected."},
    },
    conditions=[
        SIGNON_FAILED,
        {**NOT_FOUND, "armed_after": "s7"},
        outcome_cond(
            "cond_supervisor_required",
            "s8",
            region="outcome_supervisor",
            patterns=["is not authorized to perform this function"],
            code="SUPERVISOR_REQUIRED",
        ),
    ],
    outputs={"confirmation_number": text_output("Confirmation:", region="identity")},
    checkpoint={
        "all": [
            heading("Account Hold Applied"),
            {"kind": "region_text_matches_param", "region": "identity", "param": "member_id"},
        ]
    },
)


def main() -> None:
    GEN.mkdir(parents=True, exist_ok=True)
    for name, doc in ARTIFACTS.items():
        cap = Capability.model_validate(doc)
        if any(s.risk == "risky" for s in cap.steps):
            cap = sign_risk_review(cap, "ashish")
        (GEN / f"{name}.json").write_text(dump_capability(cap))
        risky = [s.id for s in cap.steps if s.risk == "risky"]
        print(
            f"wrote {name}: {len(cap.steps)} steps, risky={risky}, "
            f"outcomes={list(cap.outcomes)}, outputs={list(cap.outputs)}, "
            f"signed={cap.risk_review.reviewed_by}"
        )


if __name__ == "__main__":
    main()
