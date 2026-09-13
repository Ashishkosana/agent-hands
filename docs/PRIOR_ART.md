# Prior art for the verifier-twin work

This file exists so nobody on this project overclaims. The ideas the
verifier twin (`docs/VERIFIER_TWINS.md`) is built on are established; the
part we believe is less well covered is narrow and stated at the end.

Every entry below was checked against the linked source on 2026-09-12.
Nothing here is cited from memory, and no arXiv identifiers are used because
none of these sources are arXiv preprints.

## Established: verifying an agent's claim against the system of record

**Postcept — "Proof-of-Completion" / outcome verification for AI agents.**
<https://postcept.com/> · Python client <https://pypi.org/project/postcept/0.3.1/>
· relay <https://www.npmjs.com/package/@postcept/relay>

What it says, in its own framing: when an agent says a refund was processed,
"that is only a completion claim"; Postcept checks the claim against the
system of record and returns `safe_to_claim_complete` with a reason, so "the
entity being checked never grades its own work." Outcomes are
`verified | incomplete | duplicated | mismatched | policy_failed`, a
`pending_finality` lifecycle means "report processing, not done", and each
verification is sealed in a signed receipt. In "Relay mode" a customer-side
component reads the system of record with credentials that never leave the
customer's environment and signs what it observed.

Relationship to this repo: the separation of *claim* from *system-of-record
observation*, the refusal to grade one's own work, the "mismatched" and
"pending/not-yet-knowable" outcomes, and an observer that only reads and only
reports what it saw are all here already. Our verdict set
(`VERIFIED_COMMITTED / VERIFIED_NOT_COMMITTED / EFFECT_MISMATCH / UNVERIFIABLE`)
is a subset of the same idea. Postcept verifies via provider APIs (Stripe
today); its receipts are signed with published keys — ours are a keyless
SHA-256 hash chain, which is weaker (tamper-evident, not non-repudiable).

**Pruvz — "System-of-Record Verification" / business evidence layer.**
<https://pruvz.ai/>

What it says: an agent saying it refunded a customer or updated a CRM record
is "a claim, not proof"; proof comes from reading trusted systems (CRM,
billing, ERP, ticketing) with read-only access; the design "separates agent
execution from independent verification"; outcomes are kept on an ordered,
append-only evidence trail; "missing evidence", "policy exception" and
"outcome mismatch" are routed to human review rather than passed.

Relationship to this repo: the execution/verification split, read-only
verifier, append-only evidence, and routing missing-evidence cases to humans
instead of to a confident answer are the same commitments the reconciler and
`UNVERIFIABLE` verdict encode. Pruvz is non-blocking and asynchronous and
integrates via API/SDK against systems that expose APIs.

## Established: ambiguous outcomes after a side-effecting call

**Shengyao Sun, "Did It Happen? Counterfactual Evaluation of LLM Agent
Recovery from Ambiguous Tool Outcomes." Research Square preprint, 2026-08-19.
DOI 10.21203/rs.3.rs-10730245/v1.**
<https://www.researchsquare.com/article/rs-10730245/latest.pdf>
· benchmark <https://github.com/shushuyang231/ambiguous-tool-outcomes-benchmark>

From the abstract: "A timeout after a side-effecting tool call does not
reveal whether the action failed before execution or executed successfully
and only lost its response. Blindly retrying is correct in the former case
and can duplicate the effect in the latter; stopping has the opposite failure
pattern." The benchmark pairs the two hidden commit states behind an
identical agent-visible timeout, which gives recovery without any
information-bearing affordance an exact 0.5 exactly-once ceiling. In the main
cell, prompt-only advice scored 0.4979, status reconciliation 0.8066, and a
stable idempotency contract 1.0000.

Relationship to this repo: this is precisely the executor's situation in
fault modes A and B (byte-identical `unresolved` results, different ground
truth), and it is why the executor is forbidden from deciding. Our verifier is
a "status reconciliation" affordance in the paper's terms. MERIDIAN offers no
idempotency contract, so the paper's best-scoring lever is unavailable to us;
what we can do is make the status observable through the same UI and refuse
when it is not.

**Pat Helland, "Idempotence Is Not a Medical Condition." Communications of
the ACM 55(5):56–65, May 2012. DOI 10.1145/2160718.2160734.**
<https://cacm.acm.org/practice/idempotence-is-not-a-medical-condition/>

"Messages may be retried. Idempotence means that's OK." The paper lays out
why a first message to a service must be idempotent (it may be retried to
cope with transmission failure) and how "plumbing" can perform duplicate
elimination once enough is known about the destination. Legacy UI-only
systems supply neither the idempotent first message nor the plumbing, which
is why V1 emits `retry_eligible` as advice and never retries by itself.

**Modern Treasury API documentation, "Idempotent Requests."**
<https://docs.moderntreasury.com/platform/reference/idempotent-requests>

A representative production treatment of the same problem at the API layer:
an `Idempotency-Key` header so that "if you are creating a Payment Order and
the request fails due to a network issue, you can retry the request with the
same idempotency key to guarantee the payment order was only created once";
results cached 24h and only if the request executed; a `409` when a
concurrent request with the same key is in flight. This is the affordance a
teller console does not have.

## What we believe is the less-covered corner

Every source above verifies through an API or a data store the verifier can
query directly. In our setting **both the consequential action and the
independent observation have to be performed by computer-use against the
same legacy, UI-only application**, with:

- no idempotency key, no status endpoint, no event feed — the only readback is
  a rendered member-record table;
- the observer therefore subject to the same UI-drift, session and
  availability failures as the executor, so the observation itself can fail
  and must produce a fourth outcome (`UNVERIFIABLE`) rather than a guess;
- the executor's own transcript inadmissible as evidence of commit, because
  the acknowledgement is what the fault destroyed.

We do **not** claim that the verdict taxonomy, the pre/post-image
reconciliation, signed/hashed receipts, or the "claim is not proof" framing
are new — the sources above establish all of them. We do not claim to have
surveyed the field exhaustively; a closer match may exist. What is in this
repository is a working, measured instance of the established pattern in the
one place the existing tools do not reach: a deterministic UI replayer acting
as a read-only oracle for another deterministic UI replayer, evaluated under
injected lost-acknowledgement faults against a MERIDIAN-shaped fixture and
validated with one live run each of fault modes A, B and C against the
MERIDIAN demo (`evals/twin_results.md`; mode D was exercised locally only).
