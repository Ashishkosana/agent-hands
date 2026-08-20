# agent-hands — Record-once / Replay-many Computer-Use Automation for Legacy Banking UIs

**Author:** Ashish Kosana
**Date:** 2026-08-19
**Status:** Draft
**Reviewers:** interface.ai build-session panel (CTO, Head of Product, engineers who reviewed the take-home)

> Living document. High-level sections are complete; the per-module deep-dives
> under **Proposed Design** grow as we walk each file (requirement → why → gap).

---

## Background

interface.ai builds AI agents for banks and credit unions. Those agents decide
*what* to do; they still need a way to actually *operate* the institution's
back-office applications. When an app has an API, you integrate through it —
that is out of scope. The hard reality is the long tail of **legacy apps with
no API**: core banking screens, servicing tools, admin consoles where the only
way in is to drive the UI the way a human operator would.

Three properties of that environment shape everything:
- **Stable UIs, but real runtime errors.** The screens change slowly; the hard
  part is the exceptional states that legitimately occur — validation errors,
  "record not found," permission denials, confirmation dialogs, session expiry,
  transient slowness, app errors.
- **Heterogeneous, often legacy surfaces.** Modern web, legacy web (framesets,
  nested tables, no test IDs), or native desktop. No clean DOM, no stable
  selectors, no API.
- **Multi-tenant at scale.** Hundreds of institutions, ~20 apps each; many run
  the *same vendor product* branded/configured differently.

## Problem Statement

Build the layer that gives an AI agent *hands*: an LLM works out how to complete
a task inside a real UI **once** (discovery); that successful run is recorded as
a typed, versioned, reusable **capability**; production invocations **replay**
that capability deterministically with **no model in the decision loop**, while
handling runtime errors, escalating to a human when stuck, and staying inside
safety guardrails.

> The through-line: **the model discovers; the artifact becomes a reusable
> capability; deterministic replay is how the agent invokes it in production.**

## Goals

Five invariants the whole design serves:
1. **Never guess.** Ambiguity at replay is a failure, not a heuristic.
2. **Wrong-but-confident is the worst outcome.** Uniqueness ≠ correctness; every
   match and extracted value is verified against recorded expectations.
3. **A business outcome is an answer, not an error.** The result contract makes
   the distinction structural; a caller can enumerate every code it may receive.
4. **Irreversible actions are never auto-retried and never replayed unreviewed.**
5. **Replay sends nothing to any model, ever** — proven hermetically, not asserted.

## Scope

### In Scope
- One concrete surface: **web, via Playwright**, against a self-authored legacy
  fixture ("Fairview Teller Console").
- Discovery (LLM observe→decide→act loop) producing a capability artifact.
- Deterministic replay + a five-way result/error taxonomy.
- Human-in-the-loop escalation & handoff on the **replay** side.
- Safety: network-layer allowlist, mutating-by-default risk gate with hash-bound
  sign-off, secret/PII redaction.
- Evidence/observability (JSONL trace + failure screenshot/a11y snapshot) and a
  measured eval suite.

### Out of Scope (design-only or deliberately cut)
- **Desktop surface** — schema is surface-neutral; only `WebSurface` is built.
- **Multi-tenant** overlays / variants / drift aggregation — designed, not built.
- **Native JS dialogs** (`alert`/`confirm`) — described in design, **not built**.
- **Discovery-side escalation** — a stuck planner ends the run (replay-side is
  fully wired).
- Cut demo legs: public-site portability (saucedemo), a second tenant skin.

## Requirements

### Pre-requisites
- Python ≥ 3.12.
- Playwright Chromium browser installed.
- For **discovery only**: an API key for an OpenAI-compatible endpoint
  (Groq-hosted `openai/gpt-oss-120b` by default), in `.env` as `GROQ_API_KEY`.
- Replay, tests, and evals require **no key and no network beyond localhost**.

### Assumptions
- Target UIs are stable and change slowly (what makes record-once/replay-many
  viable). The interesting failures are runtime states, not layout drift.
- Discovery is a **privileged, attended, ~once-per-capability** event, run
  against UAT/training environments with synthetic data where possible.

### User Requirements
The "users" are a **calling AI agent** and a **human operator/reviewer**:
- An agent can invoke a capability *blind* from its contract (typed parameters,
  typed outputs, closed set of business-outcome codes) without reading the steps.
- A human reviewer can read a diffable artifact and sign off its risk labels.
- A human operator can take control of the *live session* when the system is
  stuck, act, and hand control back.

### Technical Requirements (from the brief §3, each mapped to how we satisfy it)
- **3.1 Goal-driven agent loop** — LLM observe→decide→act against a live UI,
  tool-forced, grounded by observation refs. → `planner.py`, `observe.py`.
- **3.2 Structured artifact** — typed, versioned, reviewable capability with
  steps, target identification, typed params/outputs, checkpoint. → `artifact.py`.
- **3.3 Deterministic replay + error taxonomy** — no model; business outcome vs
  recoverable vs hard failure, structured result. → `replay.py`, `results.py`.
- **3.4 Safety & policy** — allowlist, safe/risky split handled conservatively,
  no secret/PII persistence. → `surface.py` (allowlist), `replay.py` (risk gate),
  masking throughout.
- **3.5 Evidence/observability** — structured log + richer signal on failure. →
  `trace.py`, `surface.capture_evidence`, `/evidence/`.
- **3.6 Escalation & handoff** — detect stuck, route intervention with context,
  take over the live session, resume. → `escalation.py`, `replay.py`.
- **3.7 Heterogeneity & multi-tenant (design-only)** — surface seam + overlay/
  variant/drift design. → `surface.py` seam; design in this doc + REPORT §4.

### Key Constraints
- No clean DOM, no stable selectors, no test IDs on the target.
- Regulated financial data: **never persist secrets or raw PII** to artifacts or
  logs.
- **Wrong-but-confident** results are the worst failure mode.
- Irreversible actions must be structurally incapable of double-firing.
- The replay path must be **provably** model-free.

## Existing Architecture

Single process. Two execution paths joined by one seam and shared cross-cutting
concerns.

```
DISCOVERY (once, LLM)                         REPLAY (many, no LLM, proven)
 request.json (contract seed)                  capability.json + params
   planner: observe→decide→act (tool-forced)     engine: requires → per step:
   recorder: distill + round-trip-verify         pre → locator ladder → act(retry)
   ────────────────────────────►                 → post, recognizers polled →
   capability artifact (JSON)  ─────────────────► checkpoint(identity) → outputs
                                                   ↓
                              SUCCESS | BUSINESS_OUTCOME | FAILURE
                              | PRECONDITION_FAILED | POLICY_VIOLATION
 Seam:  WebSurface (only module that touches the browser)
 Cross-cutting: policy (allowlist + risk gate) · trace (JSONL + masked evidence)
                escalation (pause → human on the SAME live session → resume)
```

Module map (`src/hands/`): `cli.py` (entry/router), `artifact.py` (schema +
load-time validators), `results.py` (5-way result union), `replay.py` (engine),
`surface.py` (perceive/act seam), `conditions.py` (sensors), `values.py`
(placeholders/normalization/money parsing), `observe.py` / `planner.py` /
`recorder.py` / `discover.py` / `llm.py` (discovery), `escalation.py` (control
token + console), `trace.py` (audit log).

## Proposed Design

> This section grows as we walk each file. Each subsection follows
> **① requirement it serves → ② why built this way → ③ gap**.

### `cli.py` — command entry / router
- **① Serves:** correctness of the core loop (entry point), code quality.
- **② Why:** thin router; all logic in the engine so it's testable without the
  CLI. Exit codes encode the taxonomy (0 = success *or* business outcome; 1 =
  failure/precondition; 2 = usage). Discovery is imported lazily so the replay
  path never loads the model client (supports the zero-LLM proof).
- **③ Gap:** no capability *catalog / callable API* (brief stretch goal #1).

### `artifact.py` — the capability schema (the contract)  *(expanding)*
- **① Serves:** system design (the #1-weighted item — the artifact schema).
- **② Why:** contract-first — `parameters`/`outputs`/`outcomes` at the top so a
  caller invokes blind; steps/ladders/conditions below. `extra="forbid"` makes
  typos fail loudly at load. Load-time validators enforce: outcome-code closure
  both ways, checkpoint identity-binding, every param used, region refs resolve,
  no secrets persisted. Locator **ladder** (role+name → label → text → geometric
  → CSS last-resort) with a verified **fingerprint**. Hash-bound `risk_review`.
- **③ Gap:** no per-tenant overlay addressing yet (design-only).

*(replay.py, surface.py, conditions.py, values.py, and the discovery modules to
be added as we walk them.)*

## Alternatives Considered
- **Screenshot + coordinate replay** — coordinates are the brittleness the brief
  warns about, and re-finding targets each run puts a model back in the loop.
  Kept only as an evidence/last-rung idea for custom-drawn desktop controls.
- **CSS/XPath as primary locators** — assumes a clean DOM that legacy apps lack.
  Kept as a flagged, same-tenant-only last rung.
- **Transcript-as-artifact** — not typed, not reviewable, not parameterizable.
- **Agent frameworks** — the loop is a few hundred lines; a framework adds a
  dependency to defend without adding capability.
- **A database for artifacts** — JSON-in-git is diffable, reviewable, versioned.
- **Services/queues** — single process; the brief does not reward scaling infra.
- **Provider choice** — an OpenAI-compatible client (Groq-hosted open-weights by
  default); discovery succeeding on a modest model is evidence the *artifact*,
  not the model, carries reliability.

## Open Questions
- Which extension to build Friday (candidates: capability catalog/API; native
  dialog handling; overlay loader + a second tenant variant).
- Native dialog model: expose `alert`/`confirm` as a first-class condition kind
  vs. handle at the surface only.
- Overlay merge semantics (override/append/disable) — level of detail to build.
- Element-level taint masking vs. current wholesale evidence suppression.

## Testing

### Automation
- **85 pytest tests** (schema, ladder resolution, taxonomy, escalation, policy,
  values, discovery-offline).
- **Hermetic zero-LLM proof** (`test_zero_llm.py`): full replay in a fresh
  subprocess with key-shaped env vars stripped and non-loopback sockets blocked;
  asserts success and zero model events in the trace.
- **Eval runner** (`evals/run_evals.py`) regenerates `evals/results.md`, offline.
- **Static:** `ruff check .` and `mypy` (strict) must stay clean.

### Metrics
- Discovery cost ~3.5k tokens **once**; replay **0 tokens** (hermetically proven).
- Replay stability **10/10**; 10 injected runtime states each classified
  correctly.
- Per-step **rung-hit telemetry** in every trace (CSS rung expected at zero) as
  the drift early-warning signal.

## Operational Support

### New Issues
Every hard FAILURE carries step id, intent, expected-vs-observed, and masked
evidence (screenshot + a11y snapshot) — enough to promote an unrecognized
blocking state into a recognizer or overlay entry.

### Feature Gating
Unattended replay of a capability with risky steps is gated on a human-signed,
**hash-bound** risk review (`hands review`); editing the artifact voids approval.
Conditions carry `provenance` and `verified_by_eval` flags.

### Notify Partners
N/A (single-candidate build).

### Tooling
CLI (`hands replay|review|discover`). Future: a capability-catalog endpoint that
doubles as an agent tool surface (the schema already reads as a tool definition).

## Launch Plan (production storage story)
- **Artifacts live in git** — reviewed (risk sign-off = a PR), versioned (callers
  pin `name@version`), diffable, promotable; CI runs the load-time validators.
- A thin **registry** maps `(name, version, tenant)` → content hash; blobs in
  object storage keyed by sha256; every trace already logs that hash.
- **Not in git:** per-run traces (object storage + retention), drift/health
  telemetry, and promotion state (a database keyed by artifact hash).

## Diversity and Accessibility Considerations
The primary locator strategy is the **accessibility tree** (role + accessible
name) — the same representation screen readers use — which aligns the automation
with accessible markup and is more stable than raw DOM on legacy surfaces.

## Risks
- **Wrong-but-confident answer** (mitigated: fingerprint verification, identity-
  bound checkpoint, strict money parser, never-guess ladder).
- **Double-firing an irreversible action** (mitigated: risky steps never
  auto-retried; effect-probe before any re-act; forward-scan resume).
- **Page-native PII reaching the model during discovery** (mitigated by
  containment: discovery is once-per-capability, attended, zero-retention terms;
  replay sends nothing — you cannot redact what you cannot classify).
- **Over-flagging risk** (any non-GET → risky) — accepted; a human downgrades at
  review; the inverse (under-flagging a money move) is unacceptable.
- **Generalization unproven** (#7 is argued, not demonstrated) — the biggest
  review-question risk.

## Cleanup
None outstanding. `runs/` is transient (gitignored); curated runs are copied to
`/evidence/`.

## Tasks / Future Work
In priority order (from REPORT §7):
1. Overlay loader + a structurally divergent second tenant (make §4 demonstrated).
2. Discovery-side escalation (same hub, one integration short).
3. Element-level taint masking (replace wholesale evidence suppression).
4. Capability catalog endpoint (agent-facing tool surface).
5. Native JS dialog handling (a brief-named runtime state currently unbuilt).

## Appendix

### Integration Details
Provider seam is one thin client for any OpenAI-compatible endpoint
(`llm.py`); swapping providers is one config change (`HANDS_MODEL` /
`--model` / base URL). An Anthropic-native key would need a small adapter (the
seam speaks `chat.completions`).

### Glossary
- **Capability / artifact** — the idea vs. the JSON file that stores it.
- **Discovery** — the one-time LLM run that learns the flow.
- **Replay** — deterministic re-execution with no model.
- **Locator ladder** — ordered targeting strategies, most-stable first.
- **Recognizer** — a scoped matcher for a known runtime state (business outcome
  or recoverable).
- **Checkpoint** — the identity-bound success condition.
- **Business outcome** — a legitimate non-success answer (e.g. MEMBER_NOT_FOUND).

### References
- Assignment: "Take-Home Project: Computer-Use Automation System" (interface.ai).
- `README.md`, `REPORT.md`, `docs/DESIGN.md`, `docs/DEFENSE.md`, `CLAUDE.md`.
- `evals/results.md`, `/evidence/`.

### Decisions Made
- Web/Playwright as the one built surface; schema kept surface-neutral.
- Contract-first, supervised discovery; model proposes, recorder verifies.
- Mutating-by-default risk posture with hash-bound human sign-off.
- JSON-in-git as the artifact store.

## Revision History

| Version | Date | Author | Description |
|---|---|---|---|
| 1.0 | 2026-08-19 | Ashish Kosana | Initial draft — high-level complete; per-module deep-dives to expand during the code walkthrough. |
