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
| **Verifier** | `verifier.py` | replay a read-only artifact in a **fresh interpreter + fresh browser context**, return raw observations | any non-GET request except the sign-on POST; classify its own observation |
| **Reconciler** | `reconcile.py` | pure function `(expected, pre, post, executor) -> Reconciliation` | I/O, retries, side effects |

`twin.py` is the orchestrator that sequences them and writes the evidence
bundle. It owns no judgment: a run either becomes a `Reconciliation` or is
`REFUSED` before anything is posted.

## Executor: the sixth result, `Unresolved`

The five-way result contract gained `Unresolved(step_id, intent, dispatched,
report)`. It is raised only for a step whose `risk == "risky"` when the
engine can *prove* the action left the browser but cannot see its outcome:

- `WebSurface` records every non-GET request the page dispatches
  (`page.on("request")`); the engine marks the count before each step.
- Action ran and threw after dispatch → `Unresolved(dispatched=True)`.
- Action ran, postcondition never appeared → `Unresolved(dispatched=…)`.
- Recoverable condition interrupts a risky step after its action →
  `Unresolved` (the old "never auto-retried" rule now yields this result).

A risky step whose request never left the browser is still `Failure`. The
HTTP API maps `unresolved` to 409; the CLI exits 1; the dashboard shows
"Outcome unknown".

## Verifier: observations only

`observe_share_statuses(surface)` reads `Member No.` and the `Share ID`
table and returns `Observation(member_number, shares=[ShareStatus(...)])` —
status tokens only, no balances, no names. `ReadOnlyGuard` is a request
interceptor that **aborts** any non-GET request except `POST …/signon`
(classified as session establishment) and records the violation; a
verifier that tripped it reports `ok=False`.

`VerifierRunner.observe()` spawns `python -m hands.verifier` with the
artifact path and non-sensitive params on argv, the password in
`HANDS_PARAM_PASSWORD`, and parses one JSON `VerifierReport` from stdout.
The subprocess has no handle to the executor's page, context or cookies.

What this does *not* provide: credential independence. MERIDIAN's demo
operators (`teller1`, `super1`) share one password and one privilege model
we do not control. The verifier signs on as a different operator than the
executor, which proves the durable state is visible from a different session
— not that a compromised executor could not also compromise the verifier.

## Reconciler: pure and total

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

`WebSurface.start(interceptor=...)` exposes a `page.route` hook that runs
*after* the allowlist check. `FaultInjector(mode)` matches
`/members/<id>/hold/post`:

| mode | mechanism | durable state |
|---|---|---|
| `COMMIT_WITH_LOST_ACK` | `route.fetch()` — the real POST reaches the server — then `route.abort("connectionreset")`; records `server_status` | real |
| `NO_COMMIT_WITH_LOST_ACK` | `route.abort("connectionfailed")` before forwarding | real (unchanged) |
| `FALSE_SUCCESS` | `route.fulfill()` with a fabricated "ACCOUNT HOLD APPLIED" page; the POST is never forwarded | real (unchanged) |
| `VERIFIER_FAILURE` | applied to the **post-image verifier only**: member-record GET fulfilled with a 503 maintenance page | real |

The durable state is never faked. In C only the *presentation* is fabricated;
the reconciler catches it because the readback disagrees.

## Evaluation (`evals/run_twin_evals.py`)

Local: `fixture/meridian.py` is a Flask app that mirrors the live console's
markup and routes and adds two test-only endpoints, `GET /__state` (ground
truth) and `POST /__reset`. Each run starts from a reset fixture. Live: the
same runner with `--live` uses the original signed artifacts against the
real demo, where no ground-truth endpoint exists. The report keeps the two
sections apart on purpose.

## Deviations from the approved spec

- Hero artifact is named `meridian_place_hold`, not
  `meridian_place_account_hold`.
- The oracle is a **new** read-only artifact `meridian_member_record`
  (member record page, not the search results list): the search page does
  not show share status. Built from the existing recorded prefix steps in
  `scripts/build_capabilities.py`, signed by operator `cursor-agent`.
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

- **API escalation model for `unresolved`**: 409 is returned, but there is no
  operator workflow that takes the twin's `retry_eligible` advice to a human
  decision and back.
- **Checkpoint failure after a risky step's action** still reaches the twin
  as `Failure` in some paths (identity-bound checkpoint on the *next* step);
  the reconciler handles it (post-image decides) but the executor label is
  imprecise.
- **Collateral / forbidden effects**: `collateral_changes` is informational
  only; no verdict is derived from another share changing state.
- **Concurrency confound**: the live demo is shared; a third party placing a
  hold between pre- and post-image would be indistinguishable from our
  commit. Local runs are isolated per reset; live runs are not.
- **Verifier availability budget**: `step_budget_s` is a blunt instrument;
  there is no policy for *when* a second post-image read is allowed.
- **Discovery history and dashboard** for twin runs — nothing renders
  `twin.json` yet.
- **Generalization** beyond one capability/one oracle: no `effect_contract`
  schema, no settlement, no multiple verifier types (deliberately out of V1).
