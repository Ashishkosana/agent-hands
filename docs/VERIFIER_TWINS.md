# Verifier twins (V1) — what is actually implemented

Research question: when a consequential UI action's acknowledgement is lost,
can a **separately executed, read-only** capability determine whether the
durable effect happened, and **refuse to decide** when it cannot?

V1 answers this for one capability on one system: `meridian_place_hold`
(place an account hold on a member's share) on MERIDIAN CORE, with
`meridian_member_record` (open the member record and read the SHARES /
BALANCES table) as the oracle. Results: `evals/twin_results.md`.

## Three authorities

| authority | module | may do | may not do |
|---|---|---|---|
| **Executor** | `ReplayEngine` (`replay.py`) | replay the signed mutating artifact | decide whether an unacknowledged action committed |
| **Verifier** | `verifier.py` | replay a read-only artifact in a **fresh interpreter + fresh browser context**, return raw observations | any non-GET request except `POST /signon` (exact path); classify its own observation |
| **Reconciler** | `reconcile.py` | pure function `(expected, pre, post, executor) -> Reconciliation` | I/O, retries, side effects; **causal attribution** (see below) |

`twin.py` is the orchestrator that sequences them and writes the evidence
bundle. It owns no judgment: a run either becomes a `Reconciliation` or is
`REFUSED` before anything is posted.

## Executor: the sixth result, `Unresolved`

The result contract is six-way: `SUCCESS`, `BUSINESS_OUTCOME`, `FAILURE`,
`PRECONDITION_FAILED`, `POLICY_VIOLATION`, and `UNRESOLVED`
(`Unresolved(step_id, intent, dispatched, report)`).

**The rule.** `UNRESOLVED` requires credible evidence that a consequential
business mutation may have been dispatched without a definitive observed
outcome. Concretely:

- `WebSurface` records every non-GET request the browser **context**
  dispatches (`context.on("request")`, so popups and new tabs count) and
  classifies each as `kind="session"` — its path is exactly one of
  `PolicySettings.session_establishment_paths`, default `["/signon"]` — or
  `kind="mutation"`. Authentication is modeled separately from business
  effect: a sign-on POST is never evidence of a mutation. A failed sign-on
  (bad credentials) is therefore a `FAILURE`, even though the sign-on step
  is labeled `risky` in every planner-derived artifact.
- When a step with `risk == "risky"` is observed dispatching a
  `mutation`-kind request, the engine records a **run-scope consequential-
  action hazard** (`_Hazard(step_id, intent, requests)`) and traces
  `consequential_dispatch`. The hazard is never cleared by later steps.
- From then on, **every** `_StepFailed` — the risky step's own postcondition,
  a later step, the identity checkpoint, output extraction, a recovery or
  handback whose resume point is at or before the risky step — ends the run
  as `Unresolved`, named for the consequential step (`step_id`/`intent`) and
  carrying the actual failure in `report`. A `FAILURE` is a claim that
  nothing happened; after a mutation left the browser the engine has no
  basis for that claim.
- A **recognized business outcome** after the dispatch is a definite server
  answer and stands: `TRANSACTION_REJECTED` (the console's refusal wording,
  captured live) and `SUPERVISOR_REQUIRED` are `BUSINESS_OUTCOME`, never
  `UNRESOLVED`.
- A risky step whose action ran but dispatched no mutation (a click the page
  swallowed, a sign-on that bounced) is a plain `FAILURE`. The "never
  auto-retried" rule for risky steps is unchanged; only the label of the
  refusal depends on whether a mutation was observed.

The HTTP API maps `unresolved` to 409; the CLI exits 1; the dashboard shows
"Outcome unknown".

## Verifier: observations only

`observe_share_statuses(surface)` reads `Member No.` and the `Share ID`
table and returns `Observation(member_number, shares=[ShareStatus(...)],
observed_at, page_url)` — status tokens only, no balances, no names.
`ReadOnlyGuard` is a request interceptor installed **context-wide** that
**aborts** any non-GET request except a `POST` whose path is exactly a
session-establishment path (`/signon`; `/members/1/signon` and
`/signon/x` are blocked) and records the violation; a verifier that tripped
it reports `ok=False`.

`VerifierRunner.observe()` spawns `python -m hands.verifier` with the
artifact path and non-sensitive params on argv, the password in
`HANDS_PARAM_PASSWORD`, and parses one JSON `VerifierReport` from stdout.
The subprocess has no handle to the executor's page, context or cookies.

What this does *not* provide: credential independence. MERIDIAN's demo
operators (`teller1`, `super1`) share one password and one privilege model
we do not control. The verifier signs on as a different operator than the
executor, which proves the durable state is visible from a different session
— not that a compromised executor could not also compromise the verifier.

## Reconciler: pure and total — and window-scoped

```
missing pre or post, identity mismatch, target share absent,
pre != from_status, unknown post token            -> UNVERIFIABLE
post == to_status: executor said failure/refused  -> EFFECT_MISMATCH
                   otherwise                      -> VERIFIED_COMMITTED
post == from_status: executor said success        -> EFFECT_MISMATCH
                     otherwise                    -> VERIFIED_NOT_COMMITTED
                                                     (retry_eligible unless a
                                                      business outcome blocked it)
```

An `unresolved` executor can never produce `EFFECT_MISMATCH`: it made no
claim. `retry_eligible` is advice; nothing in the repository acts on it.
`inputs_sha256` hashes the four inputs so a decision can be recomputed.

**What a verdict means.** The evidence is two independent reads of the same
record bracketing the executor. Every `Reconciliation` therefore carries
`attribution = "window"`, `pre_observed_at`, `post_observed_at` and
`observation_window_ms`. `VERIFIED_COMMITTED` means *the target was observed
to move `from_status → to_status` inside that window*. It does **not**
establish that this executor invocation caused the move: another actor can
mutate the same target inside the window, and MERIDIAN's UI exposes no
transaction id, correlation token or per-hold operator that would let the
two be told apart. The reason strings say exactly this. Eval scenario H
(`THIRD_PARTY_MUTATION`: the executor's POST is dropped and a separate
session places the hold) yields `VERIFIED_COMMITTED` — correct about the
window, and not a claim about the executor. Causal correlation is a
separate, unbuilt mechanism (backlog).

## Pre-image / post-image protocol (`twin.py`)

1. Verifier reads the member record → `pre.json`. If it fails, or the target
   share is not in `from_status` (already `HOLD`), the run is **REFUSED** and
   the executor never starts.
2. Executor replays `meridian_place_hold` → `executor.json`
   (`masked_result`, no sensitive params).
3. Verifier reads again in a new interpreter → `post.json`.
4. `reconcile(...)` → `reconciliation.json`.
5. `manifest.json`: fault mode, chaos records, artifact hashes, run dirs,
   SHA-256 of every trace, and `verify_chain` results; `trace.jsonl` is the
   twin's own hash-chained event log.

## Chaos seam (`chaos.py`)

`WebSurface.start(interceptor=...)` exposes a `context.route` hook that runs
*after* the allowlist check. `FaultInjector(mode)` matches
`/members/<id>/hold/post`:

| mode | mechanism | durable state |
|---|---|---|
| `COMMIT_WITH_LOST_ACK` | `route.fetch()` — the real POST reaches the server — then `route.abort("connectionreset")`; records `upstream_http_status` (a transport fact — **not** evidence of commit; the console answers refusals with 200 too) | real |
| `NO_COMMIT_WITH_LOST_ACK` | `route.abort("connectionfailed")` before forwarding | real (unchanged) |
| `FALSE_SUCCESS` | `route.fulfill()` with a fabricated "ACCOUNT HOLD APPLIED" page; the POST is never forwarded | real (unchanged) |
| `VERIFIER_FAILURE` | applied to the **post-image verifier only**: member-record GET fulfilled with a 503 maintenance page | real |
| `COMMIT_WITH_CORRUPT_ACK` | real POST forwarded; the **real** acknowledgement is returned with the member identity blanked, so the risky step's postcondition passes and the identity checkpoint fails | real |
| `COMMIT_WITH_CORRUPT_OUTPUT` | real POST forwarded; the confirmation-number cell is removed, so the checkpoint passes and output extraction fails | real |
| `THIRD_PARTY_MUTATION` | the executor's POST is dropped; a harness-supplied callback places the same hold through **its own** session and the real UI flow | real (changed by someone else) |

The durable state is never faked by the injector. In C only the
*presentation* is fabricated; the reconciler catches it because the readback
disagrees. F/G exist to show uncertainty surviving past the risky step; H
exists to show what window attribution does not claim.

## Evaluation (`evals/run_twin_evals.py`)

Local: `fixture/meridian.py` is a Flask app that mirrors the live console's
markup and routes and adds two test-only endpoints, `GET /__state` (ground
truth) and `POST /__reset`. Each run starts from a reset fixture. Known
fixture/live divergence, kept deliberately: the fixture refuses a re-hold of
an already-`HOLD` share with the console's `TRANSACTION REJECTED` wording
(HTTP 200); the live console accepts the re-hold idempotently with a fresh
confirmation number (probed 2026-09-13). The refusal is kept because a
definite "no" after a consequential dispatch is a case the executor must
classify as a business outcome. Live: the same runner with `--live` uses the
committed artifacts against the real demo, where no ground-truth endpoint
exists; it **refuses** unless both artifacts carry a valid human risk-review
signature. The report keeps the two sections apart on purpose, and reports
`UNVERIFIABLE` as "n/n deliberately-unavailable-verifier runs abstained"
plus a separate spurious count — never as an operational rate.

## Deviations from the approved spec

- Hero artifact is named `meridian_place_hold`, not
  `meridian_place_account_hold`.
- The oracle is a **new** read-only artifact `meridian_member_record`
  (member record page, not the search results list): the search page does
  not show share status. Built from the existing recorded prefix steps in
  `scripts/build_capabilities.py`. Its `risk_review` is **pending**
  (`reviewed_by: null`): the project invariant requires a human reviewer,
  the earlier `cursor-agent` signature was withdrawn (review finding F5),
  and no human identity is fabricated. Likewise `meridian_place_hold` was
  regenerated to add the `TRANSACTION_REJECTED` recognizer and is pending
  re-review. Tests and local evals re-sign retargeted copies for the harness
  (fixture only, by design of `retarget()`); live unattended replay refuses
  until `hands review <artifact> --operator <name>` is run by a human.
- `REFUSED` is an orchestrator state, not a fifth reconciler verdict: it
  means no reconciliation happened because the pre-image blocked execution.
- The observation is attached as `Success.observation` rather than by a
  schema-version bump; artifacts are unchanged.
- The live curl reconnaissance before V1 placed one hold by hand
  (`100987-S0070`, CN480001) to confirm the oracle; V1's own live run
  consumed `101555-CERT`. Both are now `HOLD` on the shared demo.
- `VERIFIER_FAILURE` was validated locally only (10/10); running it live
  would consume another share for no additional information.

## Backlog (recorded, not built)

Deferred by the independent review of PR #11 (not implemented there on
purpose):

- **F4 — Durable unresolved / hazard lifecycle.** V1 records uncertainty in
  the run trace (`consequential_dispatch`, `unresolved`) and in the API's
  in-memory result state. There is no durable reconciliation queue: an
  `UNRESOLVED` that nobody reconciles is forgotten when the process exits.
  Needed: persist the hazard at dispatch time (before the outcome is known),
  a state machine `UNRESOLVED → {VERIFIED_*, EFFECT_MISMATCH, UNVERIFIABLE}
  → {closed, escalated}`, and an operator surface for the twin's
  `retry_eligible` advice. This subsumes the earlier "API escalation model
  for `unresolved`" item.
- **F9 — Baseline API idempotency: TOCTOU and payload binding.** The
  `hands.api` idempotency key is checked and then the run is executed; two
  concurrent requests with the same key can both pass the check, and the
  key is not bound to the request payload, so a reused key with different
  parameters replays the first result. Needs an atomic reserve-then-run
  store and a payload hash in the key's record. Deliberately not expanded
  into this V1 PR.
- **Causal correlation.** Window attribution is what V1 can honestly claim.
  Establishing that *this* invocation caused a change needs a correlation
  handle the system of record exposes and the verifier can read back — a
  confirmation number bound to an operator/session, a per-hold journal row,
  or a transaction id — plus a protocol for what to do when it is absent.
  MERIDIAN's UI shows a confirmation number on the acknowledgement page but
  the member record does not list holds by confirmation, so the loop cannot
  be closed through the UI today. A research problem, not a patch.

Other items:

- **Collateral / forbidden effects**: `collateral_changes` is informational
  only; no verdict is derived from another share changing state.
- **Concurrency on the shared live demo**: local runs are isolated per reset;
  live runs are not, so window attribution is the most a live verdict can
  say (see above).
- **Verifier availability budget**: `step_budget_s` is a blunt instrument;
  there is no policy for *when* a second post-image read is allowed.
- **Discovery history and dashboard** for twin runs — nothing renders
  `twin.json` yet.
- **Generalization** beyond one capability/one oracle: no `effect_contract`
  schema, no settlement, no multiple verifier types (deliberately out of V1).
