"""Per-run transfer intent approval — the examiner-facing gate.

Artifact-level ``risk_review`` proves a human approved the *recipe*. This
module binds approval to *this invocation's* intent: member, from-share,
to-share, amount, capability name@version, and the effective artifact hash.

Identities here are attestations (the same model as maker-checker), not
cryptographic operator keys. Approval is local (operator console or a test
stub) — replay never consults a model.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from hands.artifact import Capability, RoleRung, Step, TextRung

# Closed set of capability names that default the intent gate ON. Explicit
# PolicySettings.require_intent_approval=True enables it for any other
# money-moving artifact (Fairview fixture transfers included).
DEFAULT_INTENT_GATED_CAPABILITIES = frozenset({"meridian_funds_transfer"})

# A step is money-moving when its recorded intent or visible target names a
# post/submit/confirm of a transfer — not a login click that happens to be
# labelled risky because it provoked a non-GET.
_MONEY_INTENT = re.compile(
    r"\b(post|submit|confirm)\b.*\b(transfer|payment|wire)\b"
    r"|\b(transfer|payment|wire)\b.*\b(post|submit|confirm)\b",
    re.IGNORECASE,
)
_MONEY_TARGET = re.compile(
    r"\bpost\s+transfer\b|\bsubmit\s+transfer\b|\bconfirm\s+transfer\b",
    re.IGNORECASE,
)
_LOGIN_INTENT = re.compile(r"sign[\s-]?on|log[\s-]?in", re.IGNORECASE)

_TRANSFER_PARAM_KEYS = frozenset({"from_share", "to_share", "amount"})


def canonical_json(obj: Any) -> str:
    """Stable JSON for hashing: sorted keys, no insignificant whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def compute_intent_hash(payload: dict[str, Any]) -> str:
    """SHA-256 of the canonical intent payload. Any field change voids it."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class IntentHashMismatch(ValueError):
    """The hash bound at pause time does not match a recomputation now."""

    def __init__(self, expected: str, actual: str) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"intent_hash mismatch: expected {expected}, recomputed {actual}"
        )


def verify_intent_hash(payload: dict[str, Any], expected: str) -> str:
    """Recompute and refuse if the bound hash does not match. Returns the hash."""
    actual = compute_intent_hash(payload)
    if actual != expected:
        raise IntentHashMismatch(expected, actual)
    return actual


def _param(params: dict[str, str], *names: str) -> str:
    for name in names:
        value = params.get(name)
        if value is not None:
            return value
    return ""


def non_sensitive_params(capability: Capability, params: dict[str, str]) -> dict[str, str]:
    """Invocation params that may enter the intent payload and the console."""
    out: dict[str, str] = {}
    for name, value in params.items():
        spec = capability.parameters.get(name)
        if spec is not None and spec.sensitive:
            continue
        out[name] = value
    return dict(sorted(out.items()))


def build_intent_payload(
    *,
    run_id: str,
    capability: Capability,
    artifact_sha256: str,
    params: dict[str, str],
    step_id: str,
) -> dict[str, Any]:
    """The fields bound into ``intent_hash``. Named transfer fields are
    lifted for the examiner prompt; ``params`` carries every non-sensitive
    invocation value so a later edit of any of them voids the hash."""
    ns = non_sensitive_params(capability, params)
    return {
        "amount": _param(params, "amount"),
        "artifact_sha256": artifact_sha256,
        "capability": capability.name,
        "from_share": _param(params, "from_share"),
        "member_number": _param(params, "member_number", "member_id"),
        "params": ns,
        "run_id": run_id,
        "step_id": step_id,
        "to_share": _param(params, "to_share"),
        "version": capability.version,
    }


def intent_prompt(payload: dict[str, Any]) -> str:
    """Plain-language question the operator console shows."""
    amount = payload.get("amount") or "?"
    src = payload.get("from_share") or "?"
    dst = payload.get("to_share") or "?"
    member = payload.get("member_number") or "?"
    display = amount if str(amount).startswith("$") else f"${amount}"
    return f"Approve transfer of {display} from {src} → {dst} for member {member}?"


def _target_labels(step: Step) -> list[str]:
    if step.target is None:
        return []
    labels: list[str] = []
    for rung in step.target.ladder:
        if isinstance(rung, RoleRung):
            labels.append(rung.name)
        elif isinstance(rung, TextRung):
            labels.append(rung.text)
    return labels


def is_money_moving_step(step: Step) -> bool:
    """Risky click that posts money — not a login, not a safe form fill."""
    if step.risk != "risky":
        return False
    if _LOGIN_INTENT.search(step.intent):
        return False
    if _MONEY_INTENT.search(step.intent):
        return True
    return any(_MONEY_TARGET.search(label) for label in _target_labels(step))


def first_money_moving_step(capability: Capability) -> Step | None:
    for step in capability.steps:
        if is_money_moving_step(step):
            return step
    return None


def is_transfer_capability(capability: Capability) -> bool:
    """Default-on set, or a capability that declares a transfer-shaped contract
    (from/to share + amount) and actually has a money-moving risky step."""
    if capability.name in DEFAULT_INTENT_GATED_CAPABILITIES:
        return True
    keys = set(capability.parameters)
    return keys >= _TRANSFER_PARAM_KEYS and first_money_moving_step(capability) is not None


def intent_approval_required(capability: Capability, require: bool | None) -> bool:
    """Resolve the tri-state policy flag.

    ``None`` (default): ON for the meridian funds-transfer path (and any
    transfer-shaped capability). ``True``: ON whenever a money-moving risky
    step exists (Fairview fixture transfers opt in this way). ``False``: off.
    """
    if require is False:
        return False
    if first_money_moving_step(capability) is None:
        return False
    if require is True:
        return True
    return is_transfer_capability(capability)
