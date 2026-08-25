"""HTTP API exposing recorded capabilities as an invocable catalog.

An agent invokes a capability BY NAME with typed args and gets a structured
result back, knowing nothing about the underlying UI. Each invocation runs
deterministic replay under the hood — the engine is UNCHANGED, so the network
allowlist, the risky-action gate, and escalation all stay intact; the wrapper
cannot be a way around a guardrail.

This module imports NO model code — the same rule the replay path lives by.
Credentials never travel in a request body: sensitive parameters are injected
from the environment (HANDS_PARAM_<NAME>) at the boundary.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request
from pydantic import TypeAdapter

from hands.artifact import Capability, dump_capability, load_capability
from hands.replay import EngineConfig, ReplayEngine
from hands.results import ReplayResult
from hands.trace import chain_tip

# The Playwright sync API is thread-affine and drives one browser at a time, so
# invocations are serialized. Production would use a pool of worker processes;
# here one lock keeps the demo honest about concurrency.
_INVOKE_LOCK = threading.Lock()

_RESULT_ADAPTER: TypeAdapter[ReplayResult] = TypeAdapter(ReplayResult)

# HTTP status per result kind. A business outcome is an ANSWER (200); a failed
# precondition or a broken flow is a 422; a refused guardrail is a 409 conflict.
_STATUS: dict[str, int] = {
    "success": 200,
    "business_outcome": 200,
    "failure": 422,
    "precondition_failed": 422,
    "policy_violation": 409,
}


def _load_catalog(gen_dir: Path, app: str | None = None) -> dict[str, Capability]:
    catalog: dict[str, Capability] = {}
    for path in sorted(gen_dir.glob("*.json")):
        try:
            cap = load_capability(path)
        except (OSError, ValueError):
            continue
        if app is not None and cap.target.app != app:
            continue  # scope the served catalog to one application
        catalog[cap.name] = cap
    return catalog


def _contract(cap: Capability) -> dict[str, Any]:
    """The caller-facing contract — no UI detail, no steps."""
    return {
        "name": cap.name,
        "version": cap.version,
        "app": cap.target.app,
        "description": cap.description,
        "parameters": {
            n: {"type": s.type, "description": s.description, "sensitive": s.sensitive}
            for n, s in cap.parameters.items()
        },
        "outputs": {
            n: {"type": o.type, "sensitive": o.sensitive} for n, o in cap.outputs.items()
        },
        "outcomes": {c: {"description": o.description} for c, o in cap.outcomes.items()},
    }


def _tool_schema(cap: Capability) -> dict[str, Any]:
    """Project the contract into an OpenAI-style tool definition an agent can
    call. Sensitive params (credentials) are omitted — they come from the
    environment, never from the agent."""
    props: dict[str, Any] = {}
    required: list[str] = []
    for n, spec in cap.parameters.items():
        if spec.sensitive:
            continue
        props[n] = {"type": "string", "description": spec.description}
        required.append(n)
    return {
        "type": "function",
        "function": {
            "name": cap.name,
            "description": cap.description,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


def _effective_hash(cap: Capability) -> str:
    """The hash of the exact contract that executed — every run is attributable."""
    return hashlib.sha256(dump_capability(cap).encode()).hexdigest()


_CURRENCY = re.compile(
    r"^\s*\$?\s*\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?\s*$"
    r"|^\s*\$?\s*\d+(?:\.\d{1,2})?\s*$"
)


def _sanitize_value(cap: Capability, name: str, value: str) -> str:
    """Input sanitizer at the boundary: a caller sending '$1,000.50' for a
    non-sensitive amount-like parameter means 1000.50 — strip currency
    dressing ($, thousands commas, surrounding whitespace) so the artifact's
    declared pattern validates the PURE number. Applied only when the raw
    value unambiguously looks like currency; anything else passes through
    untouched and stands or falls on the artifact's own pattern."""
    spec = cap.parameters.get(name)
    if spec is None or spec.sensitive:
        return value
    if _CURRENCY.match(value):
        return value.replace("$", "").replace(",", "").strip()
    return value.strip() if value != value.strip() else value


def create_api(
    gen_dir: str | Path = "capabilities/generated",
    runs_dir: str | Path = "runs",
    app_filter: str | None = None,
) -> Flask:
    app = Flask("hands-api")
    gen = Path(gen_dir)
    only = app_filter or os.environ.get("HANDS_APP")  # scope the catalog to one application
    engine = ReplayEngine(EngineConfig(runs_dir=Path(runs_dir)))

    @app.get("/capabilities")
    def list_capabilities() -> Any:
        catalog = _load_catalog(gen, only)
        if request.args.get("format") == "tools":
            return jsonify([_tool_schema(c) for c in catalog.values()])
        return jsonify([_contract(c) for c in catalog.values()])

    @app.get("/capabilities/<name>")
    def get_capability(name: str) -> Any:
        cap = _load_catalog(gen, only).get(name)
        if cap is None:
            return jsonify({"error": f"unknown capability {name!r}"}), 404
        return jsonify(_contract(cap))

    # Idempotency: a caller retrying the same logical transaction supplies an
    # idempotency_key; a repeat with the same (capability, key) returns the
    # ORIGINAL envelope instead of re-executing — a transfer cannot be posted
    # twice by a nervous retry. In-memory by design here; production would back
    # this with a store keyed the same way.
    completed: dict[tuple[str, str], tuple[dict[str, Any], int]] = {}

    @app.post("/capabilities/<name>/invoke")
    def invoke(name: str) -> Any:
        cap = _load_catalog(gen, only).get(name)
        if cap is None:
            return jsonify({"error": f"unknown capability {name!r}"}), 404

        body = request.get_json(silent=True) or {}
        idem_key = str(
            body.get("idempotency_key") or request.headers.get("Idempotency-Key") or ""
        )

        params: dict[str, str] = {
            k: _sanitize_value(cap, k, str(v)) for k, v in (body.get("params") or {}).items()
        }
        # Credentials never travel in the request body: sensitive params are
        # injected from the environment at the boundary.
        for n, spec in cap.parameters.items():
            if spec.sensitive:
                env = os.environ.get(f"HANDS_PARAM_{n.upper()}")
                if env is not None:
                    params[n] = env

        # The idempotency check, the execution, and the record are ONE critical
        # section. Checking outside the lock is a check-then-act race: two
        # concurrent requests carrying the same key both read an empty map,
        # both pass, and the lock then politely serializes two transfers
        # instead of preventing the second one. A duplicate is exactly the
        # double-click this key exists to absorb, so the second caller waits
        # for the first to finish and receives its envelope — it never executes.
        with _INVOKE_LOCK:
            if idem_key and (name, idem_key) in completed:
                envelope, status = completed[(name, idem_key)]
                return jsonify({**envelope, "idempotent_replay": True}), status

            # ONLY engine.run is guarded here. A wider net would let an
            # internal fault raise ValueError, get reported to the caller as a
            # 400 "your parameters are wrong", and skip the `completed` write
            # below — so the caller "corrects" the request, retries the same
            # key, and re-executes a transfer that already ran.
            try:
                result = engine.run(cap, params)
            except ValueError as exc:  # missing/invalid params — a caller error, not a run
                return jsonify({"error": str(exc)}), 400
            run_dir = engine.last_run_dir

            envelope = {
                "capability": cap.name,
                "version": cap.version,
                "effective_artifact_sha256": _effective_hash(cap),
                "run_dir": str(run_dir) if run_dir is not None else None,
                # The audit log's tip, carried OUT of the directory it
                # protects: an auditor who kept this envelope can reconcile it
                # against the run later via `verify_chain(dir, expected_tip=…)`.
                "audit_chain_tip": chain_tip(run_dir) if run_dir is not None else None,
                "result": _RESULT_ADAPTER.dump_python(result, mode="json"),
            }
            status = _STATUS[result.result]
            if idem_key:
                completed[(name, idem_key)] = (envelope, status)

        return jsonify(envelope), status

    return app


def main() -> None:  # pragma: no cover - convenience runner
    import argparse

    parser = argparse.ArgumentParser(description="serve the capability API")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--gen-dir", default="capabilities/generated")
    parser.add_argument("--runs-dir", default="runs")
    args = parser.parse_args()
    create_api(args.gen_dir, args.runs_dir).run(port=args.port)


if __name__ == "__main__":  # pragma: no cover
    main()
