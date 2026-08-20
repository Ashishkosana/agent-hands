# CLAUDE.md — agent-hands

Guidance for any AI session working in this repo. Read this first; it is the
map. When code and this file disagree, the code wins — tell me so I can fix
the file.

## What this is

Record-once / replay-many computer-use automation for legacy UIs with no API.
An LLM works out a task inside a real UI **once** (*discovery*); that run is
distilled into a typed, versioned **capability artifact** (JSON); production
invocations **replay** the artifact deterministically — **no model in the
decision loop at replay**, proven by a hermetic test, not asserted.

The artifact is a **contract first, a step list second**: `parameters`,
`outputs`, and a closed set of `outcomes` live at the top level so a calling
agent can invoke it blind; steps / locator ladders / recognizers are
implementation detail below that line.

## Five invariants (the whole design serves these — do not regress them)

1. **Never guess.** Ambiguity at replay is a failure, not a heuristic. A rung
   matching >1 visible element is a hard failure, never "pick the first."
2. **Wrong-but-confident is the worst outcome.** Uniqueness is not correctness;
   every resolved element is fingerprint-verified and every extracted value is
   parse-verified against recorded expectations.
3. **A business outcome is an answer, not an error.** The result contract is
   five-way (see below); a caller can enumerate every code it may receive.
4. **Irreversible (`risky`) actions are never auto-retried and never replayed
   unattended without a human-signed, hash-bound review.**
5. **Replay sends nothing to any model, ever** — enforced by `tests/test_zero_llm.py`
   (fresh subprocess, keys stripped, non-loopback sockets blocked).

If a change would violate one of these, stop and flag it — these are the point
of the project.

## Commands

Python **≥ 3.12**. Editable install with dev extras; only discovery needs a key.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/playwright install chromium

.venv/bin/pytest                      # 85 tests, offline, ~50s (spins the fixture)
.venv/bin/ruff check .                 # lint (config in pyproject)
.venv/bin/mypy                         # strict; must stay clean
.venv/bin/python evals/run_evals.py    # regenerates evals/results.md, offline

# Run the target app (terminal 1):
.venv/bin/python -m fixture.app --port 8000

# Replay (no key, no network beyond localhost):
.venv/bin/hands replay capabilities/lookup_member_balance.json --param member_id=12345
.venv/bin/hands replay <artifact> --headed          # watch the browser
.venv/bin/hands replay <artifact> --attended        # failures -> operator console (port 8321)

# Sign risk labels (binds to artifact hash; any edit voids it):
.venv/bin/hands review <artifact> --operator <name>

# Discovery (needs GROQ_API_KEY in .env; hits the model):
.venv/bin/hands discover capabilities/requests/lookup_member_balance.request.json
```

**Keys:** `.env` holds `GROQ_API_KEY=...` (gitignored, never committed). The
model client (`llm.py`) is a thin wrapper over any **OpenAI-compatible**
endpoint; default is Groq-hosted `openai/gpt-oss-120b`. Override the model with
`HANDS_MODEL` or `--model`. Sensitive discovery params come from
`HANDS_PARAM_<NAME>` env vars, never from request files.

## Architecture: two paths, one seam

```
DISCOVERY (once, LLM, costs money)        REPLAY (many, no LLM, ~$0, proven)
 request.json (contract seed)              capability.json + params
   planner: observe→decide→act (tool-forced, ref-grounded)
   recorder: distill & round-trip-verify   engine: requires → per step:
   every rung/anchor/marker live             pre → ladder → act(retry) → post,
   ─────────────────────────────►            recognizers polled throughout →
   capability artifact (JSON)                checkpoint(identity) → outputs
                                             ↓
                                  SUCCESS | BUSINESS_OUTCOME | FAILURE
                                  | PRECONDITION_FAILED | POLICY_VIOLATION
Shared: policy (network allowlist, risk gate) · trace (JSONL + masked evidence)
        escalation (pause → human on the SAME live session → resume)
```

The **Surface** (`surface.py`) is the only module that knows about Playwright.
Everything above it speaks artifact vocabulary (ladders, context paths,
conditions, regions). A desktop port is a new Surface + rung mapping, not a
rewrite — the schema is already surface-neutral.

## Module map (`src/hands/`)

| file | responsibility |
|---|---|
| `artifact.py` | **The contract.** Pydantic schema + load-time integrity validators (outcome-code closure both ways, checkpoint identity binding, param-used, region refs resolve, no secrets persisted). `risk_hash`/`sign_risk_review`/`risk_review_valid`. **Start here.** |
| `results.py` | The 5-way `ReplayResult` union. `BusinessOutcome` carries `MatchEvidence` (auditable, not a bare claim). |
| `replay.py` | **The engine (~1000 lines, the heart).** Deterministic execution; exception-driven control flow (`_Recognized`/`_StepFailed`/`_Recover`/`_PauseRequested`); two-level timeout model; freshness rule; no-double-fire; recovery; escalation glue; masking. No model, no coordinates, no `sleep`-as-sync. |
| `surface.py` | Perceive/act seam. Locator-ladder resolution (0→next rung, >1→refuse), geometric relative rungs (same-row overlap, tie=ambiguity), fingerprint verification, context-wide network allowlist, human-event capture JS (semantic only). |
| `conditions.py` | Sensors only — **never act on the page.** `state_holds`, `post_holds`, `check_recognizers` (with freshness suppression), `matching_now`. Mid-navigation error → "not yet". |
| `values.py` | `{param:name}` resolution; `normalize` (none/digits/money); strict en_US `parse_money` (rejects rather than mis-parses); `value_bound_in` (boundary-anchored identity, not bare substring). |
| `planner.py` | LLM observe→decide→act loop. One tool per turn, act-by-ref only; protocol-safe (every tool call answered on the tool channel); `{param:}` placeholders keep values out of the prompt; `_RiskWatch` marks any non-GET step risky; `_settle_post` derives postconditions from what *actually changed* — no observable effect → `DistillationError`, never an invented postcondition. |
| `recorder.py` | Distillation. `build_ladder` (candidate rungs, each round-tripped through the real engine or dropped); `build_identity_checkpoint`/`build_output`/`build_outcome_condition` (model proposes semantics, recorder proves them live + rejects any string containing a param value); supervised outputs (extracted value must match declared example). `assemble` = final validate. |
| `observe.py` | Builds the `Observation` the planner sees; per-turn `ActionableNode` refs + pinned `ElementHandle`; password fields masked; refs never enter the artifact. |
| `discover.py` | Orchestration. Contract-first `DiscoveryRequest`; one happy run (steps+checkpoint+outputs) + one run **per declared outcome** (a live-verified recognizer each); `_arming_step` refuses to arm a recognizer across divergent flows. |
| `escalation.py` | Control-token state machine (`EscalationHub`) + minimal HTTP operator console. Token is engine-owned; console posts *requests* only and never touches Playwright (sync API is thread-affine). Single owner at every moment, structurally. |
| `trace.py` | Append-only JSONL per run under `runs/`; `tail()` feeds intervention context. |
| `cli.py` | `replay` / `review` / `discover`. Exit 0 = worked (SUCCESS or BUSINESS_OUTCOME — an answer is not a malfunction); 1 = FAILURE/PRECONDITION_FAILED; 2 = usage. Discovery imports are **lazy** so replay never imports the model client. |

`fixture/` = the "Fairview Teller" target (Flask, deliberately legacy table-soup
markup, no test IDs, nav iframe; 6 injectable faults via `POST /__faults`).
`capabilities/` = artifacts + discovery requests. `evidence/` + `evals/` = run
records and measured results. `docs/DESIGN.md` = full design incl.
considered-and-rejected; `docs/DEFENSE.md` = anticipated hard Qs + honest
answers; `REPORT.md` = as-built summary.

## Mental models where the subtlety lives

- **Locator ladder** (semantics pinned in `schema_version`): role+name → label
  → exact text → geometric relative → CSS (web-only, last resort, `fragile`
  flag). Exact, whitespace-normalized; uniqueness counted over **visible only**;
  0 → next rung, >1 → hard failure. Lower rungs are fallbacks, **not
  disambiguators**. Every target carries a `fingerprint` (role/editable) verified
  on resolution.
- **Freshness rule (two halves), business outcomes only.** A recognizer already
  matching *before* a step's action is suppressed (stale state must not classify
  a new action); suppression **lifts** once that state is seen gone. Recoverable
  conditions are *never* suppressed — a blocking state present before the action
  is exactly what recovery is for. Precedence: a satisfied postcondition beats a
  simultaneously-matching recognizer.
- **No double fire.** `risky` steps are never auto-retried; on timeout they
  escalate. Before *any* re-act, the engine probes whether the effect already
  landed (`_effect_already_present`) and re-acts only if provably absent.
  Hand-back resumes via a forward position scan (`_resume_scan`), refusing to
  re-run a risky step blind.
- **Recognizer provenance & arming.** A happy run never sees a not-found page, so
  each business outcome gets its **own** discovery run; `armed_after` is the
  happy-run step it reached, valid only if the outcome run is a structural prefix
  of the happy path (`_arming_step`). Conditions carry `provenance`
  (discovered|authored) and `verified_by_eval`.
- **Checkpoint binds identity.** "Member Details is visible" only proves you
  reached *a* details page; `region_text_matches_param` proves it's the *right*
  member. Load-time validation refuses a parameterized capability whose
  checkpoint binds no param (unless `allow_unbound_checkpoint`). Output
  extraction is anchored *inside* the identity-verified region.
- **Data-independence.** No distilled string (anchor, heading, marker, URL
  pattern) may contain a run's parameter value — else it works for one
  invocation and no other. Enforced in `recorder._reject_param_text` and
  `scrub_url_pattern`.
- **Contract-first + model-proposes/recorder-verifies + supervised.** The
  request declares params/outputs/outcomes and known example values up front;
  the model only learns *how*; the recorder mechanically proves every proposed
  semantic against the live page and refuses anchors whose extracted value
  doesn't match the declared expectation.

## Conventions

- Pydantic models use `extra="forbid"` (typos fail loudly at load). All schema
  invariants live in `@model_validator`s in `artifact.py` — add new ones there,
  not in the engine.
- `replay.py` uses exceptions for control flow (the four `_`-prefixed classes).
  Keep that shape; don't return sentinels through the step loop.
- Masking discipline: sensitive params/outputs are `«masked»` in traces,
  results, and transcripts. Never widen what reaches a prompt or `runs/`.
- Traces are the audit surface — emit an event for every decision (armed,
  suppressed, fired, recovered, escalated). `evals/run_evals.py` and several
  tests assert on trace contents.
- Money/values are type-aware, never exact-string; a parse failure is a FAILURE,
  never a value.

## Built vs designed-not-built (be honest about this line)

**Built & tested:** replay engine + full 5-way taxonomy; discovery (planner +
recorder, real LLM evidence in `evidence/`); locator ladder incl. geometric
rungs + fingerprints; recognizers with provenance & arming; identity-bound
checkpoints; supervised typed outputs; network-layer allowlist; mutating-by-
default risk + hash-bound sign-off; escalation state machine + operator console
+ resume scan + TTL + redaction canary; hermetic zero-LLM proof; 10-scenario
eval table.

**Designed, not built** (see REPORT.md/DEFENSE.md — say so plainly):
- **Multi-tenant** overlays / variants / drift-telemetry aggregation. What
  exists: per-step rung telemetry, the `fragile` flag, and a drift eval (renamed
  label → loud failure).
- **Desktop surface** (schema is surface-neutral; only `WebSurface` exists).
- **Native JS dialog** (`alert`/`confirm`) handling — no `page.on("dialog")`
  exists; DESIGN describes it but it is not built.
- **`{secret:ENV_VAR}` token** — only `{param:name}` exists; sensitive values use
  the `sensitive` flag + `HANDS_PARAM_<NAME>` env, not a separate token.
- **Element-level taint masking** — evidence is suppressed wholesale during the
  human window instead.
- **Per-step `approve:` at invocation** — the gate is signed-review vs attended,
  with no per-step approval flag.
- **Discovery-side escalation** — replay-side is fully wired; a stuck planner
  ends the run rather than raising an intervention.
- Cut legs: saucedemo portability, Lakeside second tenant.

## Editing gotchas

- **Do not import from `hands.llm` (or anything model-touching) inside the
  replay path.** The hermetic test asserts no model events; `cli._discover`
  imports discovery lazily on purpose.
- Frame handles are re-walked from the page root every poll (`frame_for`) —
  never cache a frame/locator across polls; legacy pages reload frames.
- New artifact fields: add to `artifact.py` with a validator + a schema test in
  `tests/test_artifact_schema.py`; artifacts are round-tripped by
  `dump_capability`/`load_capability`.
- Dependencies in `pyproject.toml` are lower-bound only; a fresh clone today
  resolves to openai 3.x / mypy 2.x / pytest 9 and still passes. If discovery
  ever breaks on a future major, pin an upper bound rather than editing call
  sites blindly.
- Run `ruff check . && mypy && pytest` before claiming anything is done.
