# Architecture deep-dive — three pillars

Reference notes for the technical review. Every mechanism below is anchored to
the code that implements it; where a common framing of the problem does **not**
match this implementation, the difference is stated explicitly rather than
papered over — the same rule the system itself follows (fail loud, never
present a confident wrong answer).

---

# Pillar 1 — The human-in-the-loop state machine & exception handoff

## 1.1 From fundamentals: why scrapers die at the supervisor prompt

A conventional scraper models a flow as a **linear script over an assumed
happy path**. A supervisor gate breaks that model in three ways at once:

- **It is a state the script has no branch for.** The DOM it asserted on
  (`Apply Hold` → confirmation) is replaced by an authorization interstitial.
  The next locator misses, and the script raises a generic exception.
- **It is not an error.** "This operator may not perform this function" is a
  *correct answer from the core system*. Collapsing it into a stack trace
  destroys the one piece of information the caller needed.
- **It is mid-transaction.** The flow is now parked on a live, authenticated
  session with server-side state. A script that exits leaves that session
  dangling; a script that retries may re-fire a mutation.

For a regulated institution the compliance requirement is stronger than
"don't crash":

- **Segregation of duties** — a privileged action must be attributable to the
  *human* who authorized it, not to a service account that happened to hold a
  cookie.
- **Real-time human oversight** — a supervisory control that can only inspect
  logs *after* money moved is not oversight.
- **No unattended guessing** — when the system is uncertain, the only safe
  behaviour is to stop with the session intact and hand the decision to a
  person, with enough context for them to decide in seconds.

That is why handoff is a **state machine over a live session**, not an
exception handler.

## 1.2 The two-tier classification: known states vs unknown states

A critical design decision, and the first thing to say in review:

```
KNOWN, DECLARED state        →  deterministic BUSINESS OUTCOME   (no human needed)
UNKNOWN / unsafe-to-proceed  →  ESCALATION to a human            (no guessing)
```

The supervisor gate on `meridian_place_hold` is a **declared outcome**, not an
escalation: a region-anchored recognizer matches
`"is not authorized to perform this function"` inside the named region
`outcome_supervisor` and returns

```json
{ "result": "business_outcome",
  "code": "SUPERVISOR_REQUIRED",
  "evidence": { "condition_id": "cond_supervisor_required",
                "region": "outcome_supervisor",
                "matched_text": "Operator profile teller1 is not authorized …" } }
```

Deterministic, cheap, auditable, and enumerable by the caller *before* it ever
invokes. Escalation is reserved for what the artifact could **not** anticipate —
an unrecognized blocking state, a recovery that cannot proceed safely, a risky
step whose effect cannot be proven absent, or an operator pressing *Request
control* mid-run. Escalating a state you can declare is a design smell; failing
to escalate a state you cannot is a safety defect.

## 1.3 The real state machine (`src/hands/escalation.py`)

There is no `RUNNING` and no `ESCALATED_AWAITING_SUPERVISOR`. The implemented
machine is deliberately three-valued, because the invariant it protects is
"**exactly one driver of the browser at every instant**":

```python
class ControlState(Enum):        # escalation.py:41
    AUTOMATION = "automation"    # the engine drives
    PAUSED     = "paused"        # nobody drives — the intervention is open
    HUMAN      = "human"         # the operator drives

class Decision(Enum):            # escalation.py:47
    NONE = "none"; APPROVE = "approve"; HANDBACK = "handback"
    ABORT = "abort"; RESOLVE = "resolve"
```

```
                 ┌──────────── engine.park(intervention) ───────────┐
                 │                                                  ▼
   ┌─────────────────┐   request_control()          ┌────────────────────┐
   │   AUTOMATION    │ ───────────────────────────► │       PAUSED       │
   │ (engine drives) │   (flag; engine parks at     │  (no driver; TTL   │
   └─────────────────┘    its NEXT poll tick)       │   clock running)   │
        ▲     ▲                                     └────────┬───┬───────┘
        │     │                                       take() │   │ APPROVE
        │     │ HANDBACK                                      ▼   │
        │     │                                     ┌────────────────────┐
        │     └──────────────────────────────────────│       HUMAN        │
        │                                            │ (operator drives)  │
        │  resume via _resume_scan()                 └────────┬───────────┘
        │                                                     │
        └──────────  ABORT → Failure   ·   RESOLVE → BusinessOutcome
                     TTL expiry → Failure ("intervention unanswered")
```

**Legal-transition guards are enforced in the hub, not in the UI** — the
console cannot talk the engine into an illegal state:

| Request | Precondition (`escalation.py`) | Rejected with |
|---|---|---|
| `take()` | state **is PAUSED** (`:108`) | HTTP 409 |
| `decide(APPROVE)` | state **is PAUSED** (`:126`) | HTTP 409 |
| `decide(HANDBACK/ABORT/RESOLVE)` | state **is HUMAN** (`:124`) | HTTP 409 |
| `submit_command()` | state **is HUMAN** (`:116`) | HTTP 409 |
| `record_human_action()` | state **is HUMAN** (`:157`) | silently dropped |

> **Observed live in testing:** `POST /abort` while the run was `PAUSED`
> returned **409** — you must *take control* before you can abort. That is the
> guard at `:124` doing its job: an abort is an act of a driver, so you must be
> the driver first. It is not a bug; it is the segregation-of-duties model
> refusing an unattributable transition.

## 1.4 Why the token is engine-owned (the thread-affinity argument)

The console is a `ThreadingHTTPServer` on a **daemon thread** (`:227–244`). The
Playwright **sync** API is thread-affine — driving one browser from two threads
is undefined behaviour. So the architecture inverts the usual "controller tells
the worker what to do":

- The console **only writes request flags** into the hub under one lock. It
  never imports or touches Playwright.
- The **engine thread polls** those flags at every tick of loops it already
  runs (`_poll_recognizers` raises `_PauseRequested` when
  `hub.pause_requested and hub.state is AUTOMATION`) and parks itself.
- The console shows control as *granted* only after the engine has
  acknowledged by transitioning to `PAUSED`.

This is **not** an asynchronous fire-and-forget handoff; it is
**request → acknowledge → park**. The distinction is the whole safety
property: with async handoff, "the human has control" is a *hope*; with
acknowledge-then-park, two drivers on one session is **structurally
impossible**.

**The responsiveness bound, stated precisely** (an adversarial review of this
document corrected an earlier, looser claim here — worth knowing exactly):

```
takeover latency  =  time to the next poll tick   +   the in-flight action
                     (poll_interval_s = 0.25s)         type/click/select: ≤ 2s
                                                       (attempt_timeout_ms)
                                                       navigate: Playwright's
                                                       30s default (no cap passed)
```

The `≤100 ms` figure belongs to the **parked** loop (`_park_and_wait` sleeps
`min(poll_interval_s, 0.1)`) — that is how fast a parked engine reacts to an
operator *decision*, not how fast a running engine yields control.

**Where pause is NOT granted (honest coverage gaps):** the flag is polled from
`_poll_recognizers`, which runs before every act attempt (`:375`), every
postcondition poll (`:477`) and every checkpoint poll (`:901`) — but **not**
from `_check_requires` or `_run_recovery`. So an operator pressing *Request
control* during the entry-requires phase or mid-recovery waits for the engine
to re-enter a polled loop. Neither phase can post an irreversible step
(recovery steps are engine-authored and requires is read-only), so the safety
invariant holds; the *interruptibility* is simply not universal. Closing that
is two `_poll_recognizers` calls, and it is on the next-steps list rather than
being quietly claimed as done.

## 1.5 The escalation path, step by code-level step (`replay.py`)

**`_maybe_escalate()` (`:664`)** — turns a would-be `FAILURE` into an
intervention:

0. **What funnels here, exactly.** Step-loop failures, failed recoveries,
   checkpoint-phase failures and pause requests all route through
   `_maybe_escalate` (the `except` arms at `:270–300`). Two phases sit
   **outside** the funnel and terminate directly: the entry-`requires` check
   (which yields `PreconditionFailed`, a deliberately distinct result so a
   caller is not misled by a locator error on a login page) and
   **output extraction** (`:302`, after the loop) — an unparseable balance ends
   the run as a `Failure` rather than asking a human to interpret a number. That
   second one is a defensible boundary, not an oversight: a human resolving a
   *value* would be re-introducing exactly the confident-wrong-answer risk the
   parse spec exists to eliminate.
1. **No human, no escalation.** If `hub is None or settings is None`
   (unattended), re-raise `_StepFailed` — attended-ness is a *runtime* posture,
   not an artifact property (`:681`).
2. **Escalation budget.** `escalations > settings.max_escalations` →
   annotate `observed` with `"(escalation budget exhausted)"` and fail
   (`:684`). A flow cannot ping-pong a human forever.
3. **Capture evidence** — `surface.capture_evidence(run_dir)` (screenshot +
   per-frame accessibility snapshot) (`:688`).
4. **Build the `Intervention`** (`:690`) — capability + version, step id and
   intent, `reason` as *expected vs observed*, **`_masked_params`** (secrets
   never reach the console), `remaining_steps` (so the operator knows what is
   left), `trace.tail()` (the last N hash-chained events), screenshot path.
5. **Persist it** to `intervention-{n}.json` in the run directory (`:702`) —
   the intervention itself is evidence, not just UI state.
6. **`hub.park(intervention)`** → state `PAUSED`, `pause_requested` cleared,
   `decision` reset (`:705`).
7. **`trace.emit("escalation_raised", …)`** (`:706`).
8. **Enter `_park_and_wait`** (`:712`).

**`_park_and_wait()` (`:750`) — the parked engine loop.** The engine does not
block on a queue; it *owns* the wait, because it is the only thread permitted
to touch the browser:

- On the `PAUSED → HUMAN` edge it emits `control_granted` with the operator
  identity (`:767`).
- While `HUMAN`, it **drains the operator's command queue** and executes each
  command **on the operator's behalf** (`_execute_human_command`, `:793`), then
  `fill`/`click` with a 3 s timeout. This is what makes handoff drivable and
  testable **headless** without ever violating thread affinity. Typed values are
  recorded as `text_masked_length` only (`:826`).

  *Precise resolution semantics (weaker than the ladder, deliberately):* it
  walks `surface.page.frames` and takes the **first frame containing exactly
  one visible** role+name match (`:805–812`) — per-frame uniqueness with
  first-frame-wins, **not** the ladder's cross-frame refuse-on-ambiguity rule. A
  frame with 2+ matches is skipped rather than raising; only the total absence
  of any per-frame-unique match raises `SurfaceError` → traced as
  `human_command_failed`. The justification: this channel is a *human's*
  explicit instruction on a session they already hold, not an autonomous
  decision — the no-guess discipline that governs replay applies to the engine
  choosing, not to executing an operator's command. Worth stating plainly rather
  than implying the ladder's guarantee extends here.
- `RESOLVE` with an **undeclared** outcome code does not resolve: it emits
  `escalation_invalid_decision` and **stays parked** (`:777`). A human may only
  resolve into the artifact's closed outcome set — otherwise the caller's
  contract would be violated by a console text field.
- **TTL** (`:781`): on expiry, emit `escalation_ttl_expired` and raise a
  `FailureReport` — *"intervention unanswered; session closed"*. No keep-alive
  games with a bank session; an unanswered intervention **never acts alone**.

**Terminal operator exits:**

| Decision | Result | Attribution |
|---|---|---|
| `ABORT` | `Failure`, `observed = "aborted by operator 'x': <reason>"` (`:717`) | operator + reason in the trace |
| `RESOLVE` | `BusinessOutcome(code, evidence.condition_id = f"human:{operator}")` (`:727`) | **the evidence names the human as the classifier** |
| `APPROVE` / `HANDBACK` | resume automation via forward scan (`:739`) | `resume_decision` event with operator + resume index |

That `condition_id = "human:<operator>"` is worth pausing on in review: a
human-established outcome is *not* laundered into looking machine-derived. The
receipt shows a person decided it, and which person.

## 1.6 Resume: a forward position scan, not a blind retry

`_resume_scan()` (`:832`) — because **humans finish the screen they are on**,
resuming at the failed step would re-execute work against a moved-on page:

```
1. checkpoint holds?                 → return total   (the human completed the flow;
                                                        go straight to output extraction)
2. else: scan steps in REVERSE,      → return i + 1   (the furthest proven frontier)
   return after the furthest step
   whose postconditions hold
3. else: retry the current step —
   UNLESS it is `risky`              → _StepFailed:  "no completed-step frontier found
                                                      after handback; retrying a risky step
                                                      blind could double-fire it"
```

Rule 3 is the one that matters for money: after a human window, the engine
would rather **fail loudly and re-escalate** than replay an irreversible step
whose effect it cannot prove. Combined with `_effect_already_present()`
(`:642`), which probes whether an action's effect already landed *before* any
re-act, double-execution of an irreversible action is a **structural**
property, not a probabilistic one.

## 1.7 The evidence drawer, and the leak the canary caught

The dashboard's run detail is the operator's post-hoc view: per-step timing
benchmarks derived from trace timestamps, the screenshot and accessibility/DOM
snapshot, the full decision trace with `seq`/`prev` hash-chain fields, and the
trust chips (`No AI used ✓`, `Approved by … ✓`, `Recipe unchanged ✓`,
`Log tamper-evident ✓`).

One deliberate asymmetry: once a human has driven the session, **page-content
evidence is suppressed** (`replay.py:225` — `evidence_suppressed_human_window`)
rather than captured. A canary test types a sentinel during handoff and asserts
it appears nowhere under `runs/`; the first implementation **leaked** — the
failure-path accessibility snapshot still contained the human's typed text.
Suppression is the fix; element-level taint masking is the stated refinement.
Being able to say *"my own canary caught a real leak"* is worth more than
claiming the redaction was right first time.

---

# Pillar 2 — The programmatic API architecture

## 2.1 From fundamentals: why the UI must not own the engine

Coupling a conversational surface directly to browser automation fails on four
axes that matter in production:

- **Contract.** An agent needs *typed inputs, typed outputs, and an enumerable
  set of outcome codes* it can branch on **before** it invokes. A UI that
  drives a browser exposes a screen, not a contract.
- **Substitutability.** Chatbot, scheduled batch, an internal service, and a
  human all need the same entry point with the same guarantees.
- **Enforcement.** Guardrails must sit **below** every caller. If the wrapper
  can reach around the engine, the wrapper *is* the security boundary — and a
  chatbot is a terrible security boundary.
- **Auditability.** Every invocation must be attributable to the exact
  contract bytes that executed, independent of who called it.

So the API is a **thin, typed façade over the unchanged replay engine**:
capabilities in, structured results out, no UI knowledge on either side.

## 2.2 The framework choice, argued honestly

**It is Flask** (`src/hands/api.py`), not FastAPI and not Node. The reasoning
matters more than the name:

- **Zero new dependencies.** Flask was already in the project for the
  `fixture/` target app. The brief asked for a thin demo surface; adding a
  second web framework would have been unjustified surface area.
- **Async buys nothing here.** The bottleneck is not I/O concurrency — it is a
  **thread-affine synchronous browser**. An async framework would still
  serialize on the browser, while adding an event-loop/sync-driver hazard.
  Honest framing: *"async is the right answer when your bottleneck is
  concurrent I/O; mine is a single-threaded UI driver, so concurrency belongs
  at the process level, not the coroutine level."*
- **The scaling story is unchanged by the choice.** Horizontal scale is a pool
  of worker **processes**, each with its own browser, behind a queue — because
  artifacts are stateless and content-addressed. That is true whether the
  façade is Flask, FastAPI, or Express.

## 2.3 Request lifecycle: `POST /capabilities/<name>/invoke`

```
HTTP request
   │
   ├─1. CATALOG RESOLUTION      _load_catalog(gen, only)  → 404 unknown capability
   │      · loads capabilities/generated/*.json through the Pydantic schema
   │      · load-time integrity validators run on EVERY request path
   │      · HANDS_APP scopes the catalog to one institution (tenant boundary)
   │
   ├─2. IDEMPOTENCY GATE        body.idempotency_key | Idempotency-Key header
   │      · hit  → return the ORIGINAL envelope + {"idempotent_replay": true}
   │               and DO NOT execute
   │      · miss → continue
   │
   ├─3. INPUT SANITIZER         _sanitize_value(cap, name, value) per param
   │      · unambiguous currency shape → strip "$" and thousands separators
   │      · sensitive params            → returned verbatim, never rewritten
   │      · everything else             → whitespace-trimmed only
   │
   ├─4. CREDENTIAL INJECTION    for each sensitive param: HANDS_PARAM_<NAME>
   │      · the ENVIRONMENT OVERRIDES THE BODY for every sensitive param whose
   │        HANDS_PARAM_<NAME> is set (api.py:174-179 — `if env is not None`)
   │      · honest bound: with that env var UNSET, a body-supplied sensitive
   │        value is passed through verbatim; nothing rejects it. The deployment
   │        contract is "configure the credential env vars"; a hard refusal of
   │        body-supplied secrets is a one-line hardening on the next-steps list
   │      · secrets are excluded from the agent-facing tool schema, and masked in
   │        every trace, result and transcript
   │
   ├─5. SERIALIZED EXECUTION    with _INVOKE_LOCK:  engine.run(cap, params)
   │      · the engine launches its own browser for the run and closes it in
   │        `finally`; the allowlist, risk gate, recognizers, checkpoint,
   │        masking and escalation all apply UNCHANGED
   │      · ValueError (bad/missing params) → 400: a caller bug, not a run
   │
   └─6. ENVELOPE + STATUS
          { capability, version, effective_artifact_sha256, run_dir, result }
          success | business_outcome → 200   (an answer is not an error)
          failure | precondition_failed → 422
          policy_violation → 409
```

**Why `business_outcome` is 200.** "No such member" is a *successful
determination of fact*. A 4xx would tell the caller its request was malformed,
which is false, and would push a legitimate answer into generic error handling —
precisely the outcome/failure conflation the whole result contract exists to
prevent. `POLICY_VIOLATION` is 409 (conflict with a guardrail), not 403, because
nothing about the caller's credentials was wrong: the *artifact* was not
eligible to run unattended.

**Idempotency, stated with its limits.** The store is an in-process dict keyed
`(capability, idempotency_key)`, so it is per-worker and per-process — correct
for a single-worker deployment and tested (a repeated key executes **exactly
once** and returns the original envelope). Production requires a shared store
and a TTL; that is a substitution behind the same key, not a redesign. It also
composes with, rather than replaces, the engine-level no-double-fire rule:
idempotency stops a **duplicate call**, `_effect_already_present` stops a
**duplicate action inside one call**.

**Import purity as an architectural assertion.** `hands.api` imports no model
code, verified by a test that checks `sys.modules` in a **fresh subprocess**
(an in-process check false-positives once the discovery tests have run — a real
bug found and fixed). The zero-model guarantee therefore holds across the API
surface, not just the CLI.

## 2.4 The conversational gateway sits *above* the API, deliberately

`chatbot/app.py` uses a model for exactly one job: mapping an utterance to
**one capability plus typed args**, with `ask_clarification` and
`no_capability` control tools so it can decline instead of guessing. It then
calls the same HTTP API any agent would.

Three properties make that safe:

- The model never sees credentials — sensitive params are stripped from the
  tool schema and injected server-side.
- The model cannot bypass a guardrail: an unsigned risky capability surfaces as
  a `POLICY_VIOLATION` rendered in plain language, not silently promoted.
- The model is not in the **execution** path. Worst case it selects the wrong
  capability; it cannot invent a step, a locator, or an amount.

That is the precise claim to make under challenge: *"there is a model at the
front door and provably none in the decision loop."*

---

# Pillar 3 — The per-transaction hidden token (and why there is no scraper)

## 3.1 The honest correction, first

**This system does not scrape the hidden token, and by design should not.**
No code path names, reads, intercepts or reconstructs the transaction token;
there is no request interception and no hand-assembled form payload.

Grep it honestly, because a reviewer will:

```bash
rg -nw "_token" src/ tests/ scripts/ fixture/ capabilities/   # → zero matches
rg -n  "_token" src/                                          # → only prompt_tokens /
                                                              #   completion_tokens
                                                              #   (LLM usage accounting)
```

The bare identifier appears **only in documentation**. If a reviewer asks to see
the scraper, the correct answer is:

> "There isn't one — and that's the design. I drive the real control, so the
> browser serializes the form for me, hidden fields included. Scraping the
> token and re-posting it would be *more* code, *more* fragile, and *more*
> detectable."

Claiming a scraper that does not exist would be exactly the failure mode this
project is built to prevent.

## 3.2 From fundamentals: what these tokens are, and why sleeps fail

A per-transaction hidden token is a **server-issued nonce** written into the
form the server just rendered:

```html
<form method="post" action="/members/100234/transfer/post">
  <input type="hidden" name="_token" value="fe8aed19-2a4">
  <input type="hidden" name="from"   value="100234-S0070">
  <input type="hidden" name="amount" value="1">
  <button type="submit">Post Transfer</button>
</form>
```

Its purpose is to prove that **this submission came from the page this session
was just served** — the classic CSRF defence, and on a legacy core it doubles as
a single-use transaction guard against double-posting. Three properties break
naive automation:

- **It rotates.** Every render mints a new value; the old one is stale or
  already consumed.
- **It is bound.** To the session, often to the specific transaction
  parameters.
- **It is invisible.** Nothing on screen changes when it rotates, so a script
  cannot "see" that it has gone stale — it just gets a rejection.

**Why fixed sleeps fail.** `sleep(3)` encodes an assumption about wall-clock
timing that is unrelated to the actual precondition. It is simultaneously too
short (under load the review page has not rendered, so the token you read is
from the previous page) and too long (thousands of runs pay the tax). Worse, it
provides **no evidence**: when it fails you get a locator miss with no statement
of what was expected. Timing is not a synchronization primitive; **observed
state** is.

## 3.3 What the implementation actually does

```
   RECORDED RECIPE                          LIVE EXECUTION
   ───────────────                          ──────────────
   step s12  click "Continue"     ──►  resolve via ladder → click
             post: heading "CONFIRM FUNDS TRANSFER"
                 + region_text_matches_param(confirm_member, member_number)
                                    ──►  POLL until BOTH hold
                                         (the review page — and the RIGHT member's
                                          review page — is now proven on screen,
                                          which also proves the token in that DOM
                                          is the one the server just minted)
   step s13  click "Post Transfer"  ──►  surface.click(resolved)
             risk: "risky"                     │
                                              ▼
                                    the BROWSER serializes the live form:
                                    _token + from + to + amount + memo,
                                    same-origin POST, session cookie attached
             post: heading "TRANSFER POSTED"
                                    ──►  POLL until it holds
   output    confirmation_number = cell right of "Confirmation:"  (read FRESH)
```

The token is never read, stored, logged, or re-sent by this system. It travels
because the submission **is** the application's own form submission.

**The verification interceptor.** Before the irreversible click, `s12`'s
postcondition asserts not only the confirmation heading but
`region_text_matches_param(confirm_member, member_number)` — the review screen
must be showing the member the caller asked about, boundary-anchored (so member
`123` cannot satisfy a page showing `12345`). That is the cross-verification
step in the right place: **after** the server has summarized the transaction and
**before** anyone authorizes it.

## 3.4 Why this is architecturally superior to scrape-and-repost

| Scrape the token, build the POST | Drive the real control |
|---|---|
| Must model the token's lifecycle (rotation, single-use, session binding) | No token model at all |
| Must replicate every hidden field, and drift when the server adds one | All hidden fields travel automatically |
| Must reproduce headers, ordering, content-type, cookie handling | Indistinguishable from a teller — it *is* the app's form |
| Duplicates the server's contract in client code | Zero duplication |
| A silent server change becomes a wrong POST | A UI change becomes a **loud locator failure** with evidence |

The deeper point, and the one to land in review: **this is the same argument as
the whole project.** The premise is that the UI is the only supported
interface; the moment you hand-assemble requests you have re-implemented an
unsupported private API and inherited the maintenance burden the premise says
you cannot carry. Driving the surface keeps the server's contract where it
belongs — on the server.

**On "bot guards" specifically:** the posture here is *authorized automation of
a sandbox with issued credentials*, not evasion. The system does not spoof
fingerprints, randomize timing to look human, or rotate identities — it runs a
real browser performing real, attributable actions on an allowlisted host, and
every request is logged. In a banking context that is not merely simpler, it is
the only defensible posture: automation a bank can audit must be automation a
bank can *see*.

## 3.5 What replaces sleeps: the synchronization model

- **Two-level timeouts.** A short per-attempt Playwright timeout
  (`attempt_timeout_ms`) nested inside an engine-owned **step budget**
  (`step_budget_s`). A slow page consumes attempts, not correctness.
- **Postcondition polling.** `_await_post()` returns the instant every declared
  postcondition holds; on budget exhaustion it raises a `FailureReport` naming
  `expected` vs `observed` **and the URL** — a diagnosis, not a timeout.
- **Recognizers polled throughout**, before and between attempts, so a known
  interstitial appearing *between* steps is recovered or classified rather than
  smashed into an opaque failure.
- **Freshness rule** (business outcomes only): a recognizer already matching
  *before* the action is suppressed until that state is seen gone — stale text
  cannot classify a new action. Recoverable conditions are never suppressed: a
  blocking state that predates the action is exactly what recovery is for.
- **No double fire.** `risky` steps are never auto-retried (only
  `step.risk == "safe"` is retryable, `replay.py:391`); before any re-act,
  `_effect_already_present()` must prove the effect is **absent**.
- **Ladder discipline + fingerprints.** Rungs are tried in order; `0 → next
  rung`, `1 → use it`, `>1 → hard failure`. Every resolved element is verified
  against a recorded fingerprint (role, editability) — *uniqueness is not
  correctness*. The same 0/1/>1 rule governs dropdown **options**
  (`surface.select_option`), which is why an ambiguous share refused to guess in
  live testing and escalated instead.
- **Network-layer allowlist.** `page.route("**/*", enforce)` at context
  creation covers clicks, redirects, popups, subresources and iframes; blocked
  traffic surfaces as `POLICY_VIOLATION`, not a mysterious hang.

---

## Appendix A — honest bounds (surfaced by an adversarial review of this document)

These are the limits a reviewer will find if they look, so they are stated
first. Every one has a known mitigation; none is load-bearing for the safety
invariants.

**The operator console is unauthenticated.** It binds `127.0.0.1`
(`escalation.py:235`) and the operator identity is a **self-asserted form
field** with defaults (`escalation.py:275, 294–298`). Any local process could
take control and act, and attribution is only as trustworthy as the machine.
Production needs real authentication and per-operator identity — the same
substitution the risk-review signing needs (attestation → per-operator keys).
What the design *does* guarantee regardless: whoever acted is recorded, exactly
one driver held the session, and the record is hash-chained.

**The TTL keeps running through the human window.** The deadline is computed
once at park (`replay.py:763`) and never extended, so an operator who takes
control but does not decide within `ttl_s` has the run failed **under them**,
mid-window. That is the deliberate direction to fail (never hold an
authenticated banking session open indefinitely), but a production console would
show the countdown and offer a bounded, attributed extension.

**Sensitive outputs are unmasked at the API boundary — by design, with one
caveat.** The invoke envelope serializes the real `ReplayResult`
(`api.py:193`), so a `sensitive` output (a balance) reaches the authorized
caller in full; masking applies to everything **persisted** (traces, dashboard,
transcripts). The caveat worth disclosing: the in-memory idempotency cache
retains that envelope for the process lifetime (`api.py:154, 196–197`), so a
sensitive value lives in process memory longer than the request. A production
store would encrypt at rest and TTL it.

**Two different hashes, deliberately.** `effective_artifact_sha256` in the
envelope (`api.py:99–101`, identical to `run_started`'s `artifact_sha256`) is
over the **full** artifact *including* its populated `risk_review` — it answers
"which exact bytes executed". The signature's `artifact_hash` is `risk_hash`
(`artifact.py:548+`), computed with the hash slot blanked and the reviewer's
identity bound in — it answers "is this approval still valid for this content".
Conflating them in a review would be a real error: the first is attribution, the
second is authorization.

**"Tamper-evident", not tamper-proof.** The chain is keyless
(`trace.py:5–16`): an adversary who can rewrite the whole file can recompute
every `prev` and forge an intact-looking log, and tail truncation is
undetectable without an external anchor. It defeats naive edits, deletions and
reordering — which is what it claims. Mitigation: anchor the final record's hash
outside the run directory (it already travels in the invoke envelope and the
receipt) or key the chain with an HMAC.

## Appendix B — the three claims to defend, and the evidence for each

| Claim | Evidence to show |
|---|---|
| One driver on the session at every instant | `ControlState`, hub preconditions returning 409, `control_granted` emitted only after the engine acknowledges |
| No model in the decision loop | `tests/test_zero_llm.py` (fresh subprocess, keys stripped, non-loopback sockets blocked, zero model events asserted); API import-purity test |
| An irreversible action cannot fire twice | `risky` never auto-retried; `_effect_already_present` probe; `_resume_scan` refusing a blind risky retry; API idempotency key |
