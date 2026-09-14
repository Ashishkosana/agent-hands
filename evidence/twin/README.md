# V1 verifier-twin evidence

Evidence bundles for the V1 outcome-verification work
([`docs/VERIFIER_TWINS.md`](../../docs/VERIFIER_TWINS.md)). Every bundle was
written by `hands.twin`, the orchestrator, and is what
[`evals/twin_results.md`](../../evals/twin_results.md) points at. Local and
live bundles are kept in separate directories and are never summed.

## Layout of one bundle

```
<bundle>/
  twin/
    expected.json          the effect the executor intended (target share, from_status -> to_status)
    pre.json               verifier's pre-image: raw observation, status tokens only, observed_at
    executor.json          masked executor result (success | unresolved | failure | business_outcome)
    post.json              verifier's post-image, taken in a new interpreter
    reconciliation.json    the verdict, its reason, attribution="window", both timestamps, window length, inputs_sha256
    manifest.json          fault mode, whether chaos fired, artifact SHA-256s, run dirs, SHA-256 of every trace, verify_chain results
    trace.jsonl            the twin's own hash-chained event log
    twin.json              everything above in one document
  executor/                the executor run: trace.jsonl, plus failure.png / failure-a11y.txt when the run stopped
  pre_verifier/            the pre-image verifier run: trace.jsonl
  post_verifier/           the post-image verifier run: trace.jsonl (and failure artifacts in the verifier-failure scenario)
```

Read `reconciliation.json` first. Its `reason` string states what the
verdict does and does not mean; for a committed verdict it reads, in part,
"Window attribution only: this does not establish that this run caused the
change". `inputs_sha256` lets the verdict be recomputed from the four inputs
with `hands.reconcile`.

Some `executor/failure.png` files are blank white pages: the executor's
acknowledgement was aborted at the network layer by the fault injector, so
there was nothing to render when the failure capture ran. They are kept
because the manifest hashes the bundle as written.

## `local/` — MERIDIAN-shaped fixture, ground truth available

One representative bundle per scenario from the local matrix, run against
`fixture/meridian.py` with the fixture reset before each run and truth taken
from its `GET /__state` endpoint, never from the UI.

| directory | scenario | executor result | verdict |
|---|---|---|---|
| `baseline_no_fault/` | no fault | `success` | `VERIFIED_COMMITTED` |
| `commit_with_lost_ack/` | A — real POST forwarded, response dropped | `unresolved` | `VERIFIED_COMMITTED` |
| `no_commit_with_lost_ack/` | B — POST aborted before the server | `unresolved` | `VERIFIED_NOT_COMMITTED` |
| `false_success/` | C — fabricated "hold applied" page, POST never forwarded | `success` | `EFFECT_MISMATCH` |
| `verifier_failure/` | D — post-image read answered with a 503 page | `success` | `UNVERIFIABLE` |
| `pre_image_already_hold/` | E — target already `HOLD` before execution | none | `REFUSED` (nothing posted) |
| `commit_with_corrupt_ack/` | F — real commit, acknowledgement with identity blanked | `unresolved` | `VERIFIED_COMMITTED` |
| `commit_with_corrupt_output/` | G — real commit, confirmation cell removed | `unresolved` | `VERIFIED_COMMITTED` |
| `third_party_mutation/` | H — executor POST dropped; a separate session places the hold | `unresolved` | `VERIFIED_COMMITTED` |
| `bad_credentials/` | I — sign-on refused (authentication, not mutation) | `failure` | `VERIFIED_NOT_COMMITTED` |

Scenario H is the one to read if you want to see what window attribution does
not claim: the durable state moved `OPEN → HOLD` inside the window, the
verdict is `VERIFIED_COMMITTED`, and the executor did not cause it.

## `live/` — MERIDIAN CORE, no ground truth

Three single runs against the supplied hosted demo on 2026-09-12, executor
signed on as a supervisor and verifier as a teller. There is no back door to
the truth on the live system; the verdict is what the independent verifier
read, window-scoped, on a demo shared with other users.

| directory | scenario | executor | pre → post | verdict |
|---|---|---|---|---|
| `no_commit_with_lost_ack-20260912T223308Z-twin-a038d7/` | B | `unresolved` | OPEN → OPEN | `VERIFIED_NOT_COMMITTED` |
| `false_success-20260912T223330Z-twin-941518/` | C | `success` | OPEN → OPEN | `EFFECT_MISMATCH` |
| `commit_with_lost_ack-20260912T223343Z-twin-dd656d/` | A | `unresolved` | OPEN → HOLD | `VERIFIED_COMMITTED` |

These runs used a `meridian_member_record` artifact whose risk-review
signature was later withdrawn because it had been signed by a coding agent
rather than a human (review finding F5); the artifact hashes each run used are
in its `manifest.json`. Live runs now refuse unless both artifacts carry a
human-signed review.
