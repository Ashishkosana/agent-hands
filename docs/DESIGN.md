# Design — agent-hands

**One line:** an LLM figures out a UI flow once (*discovery*); the successful run is
distilled into a typed, versioned **capability artifact**; production invocations
*replay* that artifact deterministically, with no model in the decision loop.

## Architecture

```
        DISCOVERY (once, LLM, slow, costs money)      REPLAY (many, no LLM, fast, ~$0)
┌───────────────────────────────────────┐    ┌──────────────────────────────────────┐
│ goal template + example params        │    │ capability.json + invocation params  │
│   ↓                                   │    │   ↓                                  │
│ Planner (LLM, tool-forced actions)    │    │ Replay engine                        │
│   ↑ observation          action ↓     │    │   check preconditions                │
│   │                   Policy gate     │    │   resolve locator ladder             │
│   │                        ↓          │    │   act, wait on postconditions        │
│ Surface (Playwright, a11y tree) ──────┼──┐ │   poll condition recognizers         │
│   ↓                                   │  │ │   verify checkpoint, extract outputs │
│ Recorder: trace → distill ────────────┼──┴►│   ↓                                  │
└───────────────────────────────────────┘    │ SUCCESS | BUSINESS_OUTCOME | FAILURE │
                                             └──────────────────────────────────────┘
   shared by both paths:  Policy (allowlist, risk, redaction)
                          Trace  (JSONL audit log + screenshots on failure)
                          Escalation (pause → human on the SAME live session → resume)
```

Components, each independently testable:

- **Surface** — the perceive/act seam. `observe() -> Observation` (a normalized
  accessibility-tree snapshot: role, name, state, frame path), `act(Action)`,
  `screenshot()` (evidence only). One implementation: Playwright. This protocol is
  the heterogeneity answer: a desktop surface implements the same contract over
  OS accessibility APIs (UIA/AX); artifact and replay engine are unchanged.
- **Planner** — Anthropic tool-use loop. The action tool's schema *forces* every
  proposed action to be grounded in an element from the current observation and
  expressed semantically (intent + role/name target). Stop conditions: `done`
  (with checkpoint evidence), `stuck` (reason), max steps, timeout.
- **Recorder** — keeps the full redacted transcript as evidence in `runs/`;
  distills the artifact from the executed steps. Artifact ≠ transcript.
- **Replay engine** — consumes only the artifact. Zero LLM imports, proven by test.
- **Policy** — allowlist (hosts, action kinds), risk classes, redaction. Gates
  *both* paths at the Surface, not just in prompts.
- **Escalation** — control-token state machine + minimal operator console.
- **Trace** — append-only JSONL per run; an auditor can replay the story.

## The capability artifact

JSON on disk, Pydantic-modeled, diffable, reviewable. Two version fields:
`schema_version` (artifact format) and `version` (capability revision). The
parameter/output schema doubles as an agent-facing tool definition — a calling
agent can discover what a capability needs and returns without reading its steps.

```json
{
  "schema_version": 1,
  "name": "lookup_member_balance",
  "version": 1,
  "description": "Look up a member by ID and read their savings balance.",
  "target": { "app": "fairview-teller", "entry_url": "http://localhost:8000/",
              "allowed_hosts": ["localhost:8000"] },
  "parameters": { "member_id": { "type": "string", "sensitive": false, "example": "12345" } },
  "steps": [
    { "id": "s2", "intent": "Enter the member ID in the search field",
      "action": { "kind": "type", "text": "{member_id}" },
      "target": { "ladder": [
        { "strategy": "role", "role": "textbox", "name": "Member ID", "frame": ["main"] },
        { "strategy": "label", "text": "Member ID", "frame": ["main"] } ] },
      "pre":  [ { "kind": "editable" } ],
      "post": [ { "kind": "value_is", "value": "{member_id}" } ],
      "risk": "safe" }
  ],
  "conditions": [
    { "id": "not_found",
      "match": { "kind": "text", "pattern": "No member matches", "where": "main" },
      "classify": "business_outcome", "outcome_code": "MEMBER_NOT_FOUND" },
    { "id": "session_expired",
      "match": { "kind": "role_name", "role": "dialog", "name": "Session expired" },
      "classify": "recoverable", "recovery": [ /* bounded steps, e.g. click 'Continue' */ ] }
  ],
  "checkpoint": { "all": [ { "kind": "role_name_visible", "role": "heading", "name": "Member Details" } ] },
  "outputs": { "savings_balance": { "type": "decimal",
    "from": { "strategy": "relative", "anchor": { "strategy": "text", "text": "Savings" },
              "relation": "cell_right" } } }
}
```

**Locator ladder** (ordered; a rung must resolve to *exactly one* element or the
next rung is tried; exhaustion or ambiguity is a hard failure — never a guess):

1. `role` + accessible name, scoped by frame path — what a human operator sees;
   computed by the browser even on legacy markup with no ARIA and no test IDs;
   the same abstraction desktop accessibility APIs expose.
2. associated/adjacent `label` text (form controls).
3. exact visible `text` (links/buttons).
4. `relative` — position relative to an anchor found by 1–3 (e.g. "cell right of
   cell containing 'Savings'") — how you read values out of table-soup.
5. recorded CSS path, last resort only; using it emits a fragility warning in the
   trace (this is also drift telemetry — see Tenancy).

## Record path (discovery)

Input: goal *template* + concrete example params (`"look up member {member_id}"`,
`member_id=12345`). Loop: observe → model proposes a grounded action → policy
gate → execute → repeat, until `done` / `stuck` / limits. Distillation then:

- replaces exact occurrences of param values in typed text with placeholders
  (an ambiguous or absent match is a distillation error surfaced to the human,
  not a silent guess);
- attaches per-step pre/postconditions from what was actually observed;
- takes the checkpoint from the `done` call's evidence;
- marks per-step risk (planner classification + policy keyword rules).

## Replay path (production)

Input: artifact + params, validated against the parameter schema. Per step:
check preconditions → resolve ladder → act → wait (bounded, no fixed sleeps) for
postconditions, polling the artifact's condition recognizers throughout:

- **business outcome** matched → stop, return `OUTCOME(code, evidence)` — an
  answer, not an error;
- **recoverable** matched → run its bounded recovery steps, continue; retries
  are capped;
- postcondition timeout / ladder exhausted / unrecognized blocking state →
  `FAILURE(step, expected, observed, screenshot + a11y snapshot)` — or escalate
  to a human if the run is attended.

Checkpoint verified before outputs are extracted. **No LLM:** a test runs a full
replay with no API key set and asserts the model client module is never imported.

## Failure taxonomy

Three-way result contract (conflating these is the classic mistake here):

| class | meaning | examples |
|---|---|---|
| `BUSINESS_OUTCOME` | a legitimate answer the caller needs | member not found, permission denied, validation rejected |
| recoverable (internal) | handled, logged, never surfaced as failure | known interstitial, transient slow load, session-expiry continue |
| `FAILURE` | stop loudly with a debuggable report | locator not found/ambiguous, postcondition timeout, unexpected dialog, app error |

## Escalation & control transfer

Triggers: planner `stuck` (discovery), unrecognized blocking state (replay),
irreversible step awaiting approval. Mechanism: control-token state machine —
`AUTOMATION → PAUSED → HUMAN → AUTOMATION`, single owner at all times; the
engine checks ownership before every action. An intervention request (capability,
step, reason, screenshot, run id) is written and served by a minimal local
operator console: *Take control* / *Hand back*. The human drives the **same
headed browser session**; injected listeners record their actions (redacted)
into the trace. On hand-back the engine re-observes and re-verifies the current
step's postconditions to decide: advance, retry, or fail.

## Safety

- Allowlist enforced at the Surface for every navigation and action — not just
  requested of the model in a prompt.
- Risk classes on steps: `safe` vs `irreversible`. Irreversible ⇒ blocked in
  unattended replay unless explicitly approved (`--approve-irreversible`),
  escalated in attended mode; same gate during discovery.
- Redaction: parameters marked `sensitive` never appear in artifacts, traces,
  prompts (masked in observation serialization), or screenshots (suppressed on
  sensitive steps). Credentials only via environment. All fixture data is fake.

## Heterogeneity & tenancy (design-only, per the brief)

- **Surface seam:** desktop = same `observe/act` protocol over UIA/AX; the
  artifact's semantic targets (role + name) are exactly what those APIs expose.
- **Tenant reuse:** capabilities reference a logical app + semantic targets; a
  per-tenant overlay file (entry URL, label synonyms per rung, extra known
  conditions) merges at load time — record once, override narrowly.
- **Drift detection:** replay telemetry records which ladder rung matched and
  which conditions fired; when lower rungs start carrying the load for a tenant,
  that capability is flagged for re-discovery *before* it breaks.

## Eval plan

Pytest scenario suite against the local fixture app (offline, CI-able):

- happy-path replay with fresh params;
- six injected runtime states — not-found, permission-denied, validation error,
  session expiry, surprise interstitial, slow load — each asserting the *correct
  classification*, not just non-crash;
- element-removed (quality of the hard-failure report);
- one drift case (renamed label; ladder rung 2 catches it) — the brief asks
  about drift only secondarily;
- N=10 replay stability;
- negative policy tests: a fake planner proposes a disallowed / off-allowlist
  action and is blocked;
- the zero-LLM replay proof.

Reported as a table: scenario × expected vs actual classification; discovery vs
replay latency; tokens + cost (replay = 0); stability. Evidence from the real
LLM discovery run committed under `/evidence/`.

## Targets

- **Fixture:** "Fairview Teller" — small Flask app, deliberately legacy: table
  layout, no test IDs, nav iframe, server-rendered, terse non-semantic markup;
  fault-injection endpoint; fake seeded data. A second skin ("Lakeside") with
  renamed labels and an extra interstitial is the two-tenants demo.
- **Portability leg:** replay a second, simple capability against
  saucedemo.com (built for automation practice) to show nothing is welded to
  the fixture's markup. First thing cut if time runs short.

## Slices (each ends runnable + tested)

1. Fixture app + artifact schema + replay engine driven by a **hand-written**
   artifact — the production path first, fully testable with no LLM anywhere.
2. Planner + recorder: discovery produces an artifact that slice 1 replays.
   The real evidence run happens here.
3. Full failure taxonomy: fault injection, recognizers, three-way contract.
4. Escalation & handoff: control token, operator console, human-action capture,
   resume verification.
5. Policy hardening: allowlist, risk gates, redaction, negative tests.
6. Evals + evidence + README/REPORT + tenant-variant and portability demos.

## Considered and rejected

- **Screenshot + coordinate replay** — the most literal "computer use", but
  coordinates are exactly the brittleness the brief warns about, and re-finding
  targets each run puts a model back into the supposedly model-free path.
  Screenshots kept for evidence and escalation context only.
- **CSS/XPath as primary locators** — assumes the clean DOM the brief spends a
  page saying doesn't exist. Kept only as the flagged last-resort rung.
- **Transcript-as-artifact** — not typed, not reviewable, not parameterizable.
  The transcript is evidence; the artifact is a contract.
- **Agent frameworks** (LangChain etc.) — the loop is a couple hundred lines;
  a framework adds a dependency to defend without adding capability.
- **A database** — JSON files are diffable and reviewable, which is the point.
- **Services/queues** — single process; the brief explicitly does not reward
  scaling infrastructure.
