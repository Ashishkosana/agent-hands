"""Author the full MERIDIAN CORE capability registry (the 7-function surface).

Hand-authored artifacts (Path B): each reuses the DISCOVERED login+lookup
prefix from meridian_member_balance (proven live), then appends its own steps.
Everything is built through the Pydantic schema, so every integrity rule
(outcome closure, identity binding, param usage, region resolution) is
enforced at authoring time. Provenance on authored recognizers is "authored"
and verified_by_eval=False until a live run exercises them — the honest state
the schema was designed to express.

Run:  .venv/bin/python scripts/build_capabilities.py
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hands.artifact import Capability, dump_capability

GEN = Path("capabilities/generated")
BASE = json.loads((GEN / "meridian_member_balance.json").read_text())
TARGET = BASE["target"]
REQUIRES = BASE["requires"]
# The discovered, live-proven prefix: s1 operator, s2 password, s3 sign-on
# (risky), s4 member inquiry, s5 member number, s6 search, s7 select record.
PREFIX: list[dict[str, Any]] = BASE["steps"]

P_OPERATOR = {
    "type": "string", "description": "Operator sign-on ID", "sensitive": False,
    "example": "teller1", "pattern": "[a-zA-Z0-9_]{1,32}",
}
P_PASSWORD = {
    "type": "string", "description": "Operator password", "sensitive": True,
    "example": None, "pattern": None,
}
P_MEMBER = {
    "type": "string", "description": "Member number to look up", "sensitive": False,
    "example": "100234", "pattern": "[0-9]{1,10}",
}

IDENTITY_REGION = {  # the member-record header table: "Member No.: <n>  Name: <name>"
    "ladder": [{"strategy": "relative", "relation": "container_of",
                "anchor": {"strategy": "text", "text": "Member No.:"}, "container": "table"}],
    "context": [], "fingerprint": None, "fragile": False,
}
SEARCH_REGION = {  # the member-inquiry screen (where "no records matched" renders)
    "ladder": [{"strategy": "relative", "relation": "container_of",
                "anchor": {"strategy": "text", "text": "MEMBER INQUIRY / SELECTION"},
                "container": "table"}],
    "context": [], "fingerprint": None, "fragile": False,
}

NOT_FOUND_CONDITION = {  # authored twin of the discovered recognizer; same marker text
    "id": "cond_member_not_found",
    "armed_after": "s6",
    "match": {"kind": "region_text", "region": "outcome_member_not_found",
              "patterns": ["No member records matched your search."]},
    "classify": "business_outcome", "outcome_code": "MEMBER_NOT_FOUND",
    "recovery": [], "resume": None, "restart_from": None,
    "max_fires_per_run": 2, "provenance": "authored", "verified_by_eval": False,
}

# Session heal: after sign-on, the OPERATOR SIGN ON heading reappearing means
# the session died (440/idle -> redirect to /signon). Recover by navigating to
# the entry URL and restarting from s1 — the engine's arming rule disarms this
# condition while index < s3 during the re-login pass, so healing cannot loop.
def session_heal(entry_url: str) -> dict[str, Any]:
    return {
        "id": "cond_session_expired",
        "armed_after": "s4",
        "match": {"kind": "role_name", "role": "heading", "name": "OPERATOR SIGN ON",
                  "context": []},
        "classify": "recoverable",
        "outcome_code": None,
        "recovery": [{
            "id": "r1", "intent": "Return to the sign-on page",
            "action": {"kind": "navigate", "url": entry_url},
            "target": None, "pre": [],
            "post": [{"kind": "role_name_visible", "role": "heading",
                      "name": "OPERATOR SIGN ON", "context": []}],
            "risk": "safe",
        }],
        "resume": "restart_from", "restart_from": "s1",
        "max_fires_per_run": 2, "provenance": "authored", "verified_by_eval": False,
    }


def prefix_until_link(action_link: str) -> list[dict[str, Any]]:
    """The discovered prefix, with s7's postcondition strengthened to require
    the target ACTIONS link: on a large member record the page streams, so the
    MEMBER RECORD heading can be visible before the action links exist. The
    step is only 'done' when the link this capability needs has rendered."""
    steps = json.loads(json.dumps(PREFIX))  # deep copy
    steps[6]["post"].append({"kind": "role_name_visible", "role": "link",
                             "name": action_link, "context": []})
    return steps


def type_step(id_: str, intent: str, label: str, param: str, *, risky: bool = False,
              post: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "id": id_, "intent": intent,
        "action": {"kind": "type", "text": f"{{param:{param}}}"},
        "target": {"ladder": [
            {"strategy": "relative", "relation": "nearest_input_right",
             "anchor": {"strategy": "text", "text": label}, "container": None}],
            "context": [], "fingerprint": {"role": "textbox", "editable": True},
            "fragile": False},
        "pre": [{"kind": "editable"}],
        "post": post or [{"kind": "value_matches_param", "param": param, "normalize": "none"}],
        "risk": "risky" if risky else "safe",
    }


def select_step(id_: str, intent: str, label: str, option: str,
                match: str = "contains") -> dict[str, Any]:
    return {
        "id": id_, "intent": intent,
        "action": {"kind": "select", "option": option, "match": match},
        "target": {"ladder": [
            {"strategy": "relative", "relation": "nearest_input_right",
             "anchor": {"strategy": "text", "text": label}, "container": None}],
            "context": [], "fingerprint": {"role": "combobox", "editable": None},
            "fragile": False},
        "pre": [{"kind": "visible"}],
        "post": [{"kind": "option_selected", "option": option, "match": match}],
        "risk": "safe",
    }


def click_step(id_: str, intent: str, name: str, role: str, heading: str,
               *, risky: bool = False) -> dict[str, Any]:
    return {
        "id": id_, "intent": intent, "action": {"kind": "click"},
        "target": {"ladder": [
            {"strategy": "role", "role": role, "name": name},
            {"strategy": "text", "text": name}],
            "context": [], "fingerprint": {"role": role, "editable": None},
            "fragile": False},
        "pre": [{"kind": "visible"}],
        "post": [{"kind": "role_name_visible", "role": "heading", "name": heading,
                  "context": []}],
        "risk": "risky" if risky else "safe",
    }


def text_output(anchor: str, *, sensitive: bool = False,
                region: str = "identity") -> dict[str, Any]:
    return {
        "type": "string", "sensitive": sensitive, "region": region,
        "from": {"ladder": [{"strategy": "relative", "relation": "cell_right",
                             "anchor": {"strategy": "text", "text": anchor},
                             "container": None}],
                 "context": [], "fingerprint": None, "fragile": False},
        "parse": {"kind": "text"},
    }


def capability(**kw: Any) -> dict[str, Any]:
    doc = {
        "schema_version": 1, "version": 1, "target": TARGET, "requires": REQUIRES,
        "regions": {}, "conditions": [], "allow_unbound_checkpoint": False,
        "recorded_by": None,
        "risk_review": {"reviewed_by": None, "artifact_hash": None},
        "outcomes": {}, "outputs": {},
    }
    doc.update(kw)
    return doc


ENTRY = TARGET["entry"]["web"]["url"]
ARTIFACTS: dict[str, dict[str, Any]] = {}

# ─── 1. SESSION MANAGEMENT: sign on + active-session check ──────────────────
ARTIFACTS["meridian_sign_on"] = capability(
    name="meridian_sign_on",
    description=("Sign on to MERIDIAN CORE and verify the session is active "
                 "(the main menu is reachable)."),
    parameters={"operator_id": P_OPERATOR, "password": P_PASSWORD},
    steps=PREFIX[:3],  # the discovered s1-s3 login steps, verbatim
    checkpoint={"all": [{"kind": "role_name_visible", "role": "heading",
                         "name": "MAIN MENU", "context": []}]},
    # Sign-on has no member record to bind; the checkpoint proves an ACTIVE
    # SESSION (main menu), which is this capability's entire contract.
    allow_unbound_checkpoint=True,
)

# ─── 2. MEMBER LOOKUP by SURNAME (uses the select action on Search-by) ───────
ARTIFACTS["meridian_member_lookup_by_name"] = capability(
    name="meridian_member_lookup_by_name",
    description=("Sign on, search the member directory by LAST NAME, open the "
                 "unique matching record, and return the member number and name."),
    parameters={"operator_id": P_OPERATOR, "password": P_PASSWORD,
                "last_name": {"type": "string", "description": "Member surname to search",
                              "sensitive": False, "example": "Lovelace",
                              "pattern": "[A-Za-z'\\- ]{1,40}"}},
    steps=[*PREFIX[:4],
        select_step("s5", "Search by last name", "Search by:", "Last Name", match="label"),
        type_step("s6", "Type the surname", "Value:", "last_name"),
        {**click_step("s7", "Click Search", "Search", "button", "unused"),
         "post": [{"kind": "role_name_visible", "role": "link", "name": "Select",
                   "context": []}]},  # happy-only marker: results exist
        click_step("s8", "Open the matching member record", "Select", "link",
                   "MEMBER RECORD"),
    ],
    regions={"identity": IDENTITY_REGION,
             "outcome_member_not_found": SEARCH_REGION},
    outcomes={"MEMBER_NOT_FOUND": {"description": "No member matches this surname."}},
    conditions=[{**NOT_FOUND_CONDITION, "armed_after": "s7"}],
    outputs={"member_number": text_output("Member No.:"),
             "member_name": text_output("Name:")},
    checkpoint={"all": [
        {"kind": "role_name_visible", "role": "heading", "name": "MEMBER RECORD",
         "context": []},
        {"kind": "region_text_matches_param", "region": "identity", "param": "last_name"},
    ]},
)

# ─── 3. ACCOUNT INQUIRY: name, contact, and checking balance in one read ─────
ARTIFACTS["meridian_account_inquiry"] = capability(
    name="meridian_account_inquiry",
    description=("Sign on, open a member's record, and read their name, e-mail, "
                 "and checking (Share Draft) balance in one inquiry."),
    parameters={"operator_id": P_OPERATOR, "password": P_PASSWORD, "member_number": P_MEMBER},
    steps=PREFIX,  # the full discovered path to MEMBER RECORD
    regions={"identity": IDENTITY_REGION,
             "output_checking_balance": BASE["regions"]["output_checking_balance"],
             "outcome_member_not_found": SEARCH_REGION},
    outcomes={"MEMBER_NOT_FOUND": {"description": "No member exists with this number."}},
    conditions=[NOT_FOUND_CONDITION, session_heal(ENTRY)],
    outputs={
        "member_name": text_output("Name:"),
        "member_email": text_output("E-mail:", sensitive=True),
        "checking_balance": BASE["outputs"]["checking_balance"],
    },
    checkpoint=BASE["checkpoint"],
)

# ─── 5. NEW SHARE ORIGINATION (review -> post; posting is irreversible) ──────
ARTIFACTS["meridian_open_new_share"] = capability(
    name="meridian_open_new_share",
    description=("Sign on, open a member's record, and open a new share of the "
                 "given type with an initial deposit (review, then post)."),
    parameters={"operator_id": P_OPERATOR, "password": P_PASSWORD, "member_number": P_MEMBER,
                "share_type": {"type": "string",
                               "description": "Stable substring of the share type option, "
                                              "e.g. 'Money Market'",
                               "sensitive": False, "example": "Money Market", "pattern": None},
                "initial_deposit": {"type": "string", "description": "Initial deposit in dollars",
                                    "sensitive": False, "example": "10",
                                    "pattern": "[0-9]+(\\.[0-9]{1,2})?"}},
    steps=[*prefix_until_link("Open New Share"),
        click_step("s8", "Open the New Share form", "Open New Share", "link",
                   "OPEN NEW SHARE"),
        select_step("s9", "Choose the share type", "Share Type:", "{param:share_type}"),
        type_step("s10", "Enter the initial deposit", "Initial Deposit:", "initial_deposit"),
        click_step("s11", "Continue to the confirmation screen", "Continue", "button",
                   "CONFIRM NEW SHARE"),
        click_step("s12", "Post the new share", "Open Share", "button", "SHARE OPENED",
                   risky=True),
    ],
    outcomes={"VALIDATION_REJECTED": {
        "description": "The core rejected the request (e.g. below the minimum opening deposit)."}},
    conditions=[{
        "id": "cond_validation_rejected",
        "armed_after": "s11",
        "match": {"kind": "region_text", "region": "outcome_validation",
                  "patterns": ["The request could not be validated"]},
        "classify": "business_outcome", "outcome_code": "VALIDATION_REJECTED",
        "recovery": [], "resume": None, "restart_from": None,
        "max_fires_per_run": 2, "provenance": "authored", "verified_by_eval": False,
    }],
    regions={"outcome_validation": {  # the validation banner (exact live marker text)
        "ladder": [{"strategy": "relative", "relation": "container_of",
                    "anchor": {"strategy": "text", "text": "The request could not be validated:"},
                    "container": "table"}],
        "context": [], "fingerprint": None, "fragile": False},
        "identity": {  # the posted-confirmation table carries the member context
            "ladder": [{"strategy": "relative", "relation": "container_of",
                        "anchor": {"strategy": "text", "text": "Confirmation:"},
                        "container": "table"}],
            "context": [], "fingerprint": None, "fragile": False}},
    outputs={"confirmation_number": {
        "type": "string", "sensitive": False, "region": "identity",
        "from": {"ladder": [{"strategy": "relative", "relation": "cell_right",
                             "anchor": {"strategy": "text", "text": "Confirmation:"},
                             "container": None}],
                 "context": [], "fingerprint": None, "fragile": False},
        "parse": {"kind": "text"}}},
    checkpoint={"all": [
        {"kind": "role_name_visible", "role": "heading", "name": "SHARE OPENED",
         "context": []},
        {"kind": "region_text_matches_param", "region": "identity", "param": "member_number"},
    ]},
)

# ─── 6. CONTACT PROFILE MAINTENANCE (email / phone / address mutation) ───────
ARTIFACTS["meridian_update_contact"] = capability(
    name="meridian_update_contact",
    description=("Sign on, open a member's record, and update their e-mail, "
                 "phone, and mailing address."),
    parameters={"operator_id": P_OPERATOR, "password": P_PASSWORD, "member_number": P_MEMBER,
                "email": {"type": "string", "description": "New e-mail address",
                          "sensitive": False, "example": "ada@example.com",
                          "pattern": "[^@\\s]+@[^@\\s]+\\.[^@\\s]+"},
                "phone": {"type": "string", "description": "New phone number",
                          "sensitive": False, "example": "555-0100",
                          "pattern": "[0-9\\-() +.]{7,20}"},
                "address": {"type": "string", "description": "New mailing address",
                            "sensitive": False, "example": "1 Main St, Springfield",
                            "pattern": None}},
    steps=[*prefix_until_link("Update Member Information"),
        click_step("s8", "Open the Update Member Information form",
                   "Update Member Information", "link", "UPDATE MEMBER INFORMATION"),
        type_step("s9", "Enter the new e-mail", "E-mail:", "email"),
        type_step("s10", "Enter the new phone", "Phone:", "phone"),
        type_step("s11", "Enter the new mailing address", "Mailing Address:", "address"),
        # The save mutates the member record -> mutating-by-default = risky.
        {**click_step("s12", "Save the updated contact details", "Save Changes", "button",
                      "MEMBER INFORMATION UPDATED", risky=True),
         "target": {"ladder": [
             {"strategy": "role", "role": "button", "name": "Save Changes"},
             {"strategy": "text", "text": "Save Changes"}],
             "context": [], "fingerprint": {"role": "button", "editable": None},
             "fragile": False}},
    ],
    regions={"identity": {  # the CHANGES SAVED banner names the member it changed
        "ladder": [{"strategy": "relative", "relation": "container_of",
                    "anchor": {"strategy": "text", "text": "MEMBER INFORMATION UPDATED"},
                    "container": "table"}],
        "context": [], "fingerprint": None, "fragile": False}},
    outputs={},
    checkpoint={"all": [
        {"kind": "role_name_visible", "role": "heading",
         "name": "MEMBER INFORMATION UPDATED", "context": []},
        {"kind": "region_text_matches_param", "region": "identity", "param": "member_number"},
    ]},
)

# ─── 7. ACCOUNT HOLD (risky + supervisor-gated -> the escalation surface) ────
ARTIFACTS["meridian_place_hold"] = capability(
    name="meridian_place_hold",
    description=("Sign on, open a member's record, and place a hold on one of "
                 "their shares (review, then post). Supervisor-only: a teller "
                 "attempt returns the SUPERVISOR_REQUIRED outcome."),
    parameters={"operator_id": P_OPERATOR, "password": P_PASSWORD, "member_number": P_MEMBER,
                "share": {"type": "string",
                          "description": "Stable substring identifying the share, "
                                         "e.g. 'Share Draft (Checking)'",
                          "sensitive": False, "example": "Share Draft (Checking)",
                          "pattern": None},
                "reason": {"type": "string",
                           "description": "Stable substring of the hold reason option",
                           "sensitive": False, "example": "FRAUD", "pattern": None},
                "notes": {"type": "string", "description": "Hold notes",
                          "sensitive": False, "example": "requested by member",
                          "pattern": None}},
    steps=[*prefix_until_link("Place Account Hold"),
        click_step("s8", "Open the Place Account Hold form", "Place Account Hold",
                   "link", "PLACE ACCOUNT HOLD"),
        select_step("s9", "Choose the share to hold", "Share:", "{param:share}"),
        select_step("s10", "Choose the reason code", "Reason Code:", "{param:reason}"),
        type_step("s11", "Enter the hold notes", "Notes:", "notes"),
        {**click_step("s12", "Continue to the confirmation screen", "Continue", "button",
                      "CONFIRM ACCOUNT HOLD"),
         "post": [
             {"kind": "role_name_visible", "role": "heading",
              "name": "CONFIRM ACCOUNT HOLD", "context": []},
             # verification interceptor: the review is for the RIGHT member
             {"kind": "region_text_matches_param", "region": "confirm_member",
              "param": "member_number"}]},
        click_step("s13", "Apply the hold", "Apply Hold", "button", "ACCOUNT HOLD APPLIED",
                   risky=True),
    ],
    regions={
        "confirm_member": {"ladder": [
            {"strategy": "relative", "relation": "container_of",
             "anchor": {"strategy": "text", "text": "Member:"}, "container": "table"}],
            "context": [], "fingerprint": None, "fragile": False},
        "identity": {"ladder": [{"strategy": "relative", "relation": "container_of",
                                 "anchor": {"strategy": "text", "text": "Confirmation:"},
                                 "container": "table"}],
                     "context": [], "fingerprint": None, "fragile": False},
        # The supervisor gate, exact text captured live from the sandbox.
        "outcome_supervisor": {"ladder": [
            {"strategy": "relative", "relation": "container_of",
             "anchor": {"strategy": "text", "text": "SUPERVISOR OVERRIDE REQUIRED"},
             "container": "table"}],
            "context": [], "fingerprint": None, "fragile": False},
    },
    outcomes={"SUPERVISOR_REQUIRED": {
        "description": "This operator is not authorized; a supervisor must complete the hold."}},
    conditions=[{
        "id": "cond_supervisor_required",
        "armed_after": "s8",  # armed for the whole hold flow
        "match": {"kind": "region_text", "region": "outcome_supervisor",
                  # param-free marker (never the echoed operator id)
                  "patterns": ["is not authorized to perform this function"]},
        "classify": "business_outcome", "outcome_code": "SUPERVISOR_REQUIRED",
        "recovery": [], "resume": None, "restart_from": None,
        "max_fires_per_run": 2, "provenance": "authored", "verified_by_eval": False,
    }],
    outputs={"confirmation_number": {
        "type": "string", "sensitive": False, "region": "identity",
        "from": {"ladder": [{"strategy": "relative", "relation": "cell_right",
                             "anchor": {"strategy": "text", "text": "Confirmation:"},
                             "container": None}],
                 "context": [], "fingerprint": None, "fragile": False},
        "parse": {"kind": "text"}}},
    checkpoint={"all": [
        {"kind": "role_name_visible", "role": "heading",
         "name": "ACCOUNT HOLD APPLIED", "context": []},
        {"kind": "region_text_matches_param", "region": "identity", "param": "member_number"},
    ]},
)


def main() -> None:
    for name, doc in ARTIFACTS.items():
        cap = Capability.model_validate(doc)  # every integrity rule enforced here
        (GEN / f"{name}.json").write_text(dump_capability(cap))
        risky = [s.id for s in cap.steps if s.risk == "risky"]
        print(f"wrote {name}: {len(cap.steps)} steps, risky={risky}, "
              f"outcomes={list(cap.outcomes)}, outputs={list(cap.outputs)}")


if __name__ == "__main__":
    main()
